#!/usr/bin/env python3
"""Search a GKD snapshot tree and suggest selectors for candidate action nodes."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from typing import Any


SEARCH_FIELDS = ("text", "desc", "vid", "id", "name")


def load_snapshot(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if name.endswith(".json") and not name.endswith(".min.json")
            ]
            for name in candidates:
                data = json.loads(archive.read(name).decode("utf-8"))
                if isinstance(data, dict) and data.get("nodes"):
                    return data
        raise ValueError(f"No full GKD snapshot JSON found in {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise ValueError(f"Not a GKD snapshot: {path}")
    if not data["nodes"]:
        raise ValueError(f"Snapshot has no nodes; use the full JSON instead of .min.json: {path}")
    return data


def nearest_clickable(node: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    current: dict[str, Any] | None = node
    while current is not None:
        attr = current.get("attr", {})
        if attr.get("clickable") and attr.get("visibleToUser", True):
            return current
        current = by_id.get(current.get("pid"))
    return None


def _quoted(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def selector_suggestions(
    node: dict[str, Any],
    *,
    semantic_child: dict[str, Any] | None = None,
) -> list[str]:
    attr = node.get("attr", {})
    class_name = str(attr.get("name") or "*").rsplit(".", 1)[-1]
    suffix = "[clickable=true][visibleToUser=true]" if attr.get("clickable") else "[visibleToUser=true]"
    selectors = []
    if attr.get("vid"):
        selectors.append(f"@{class_name}[vid={_quoted(attr['vid'])}]{suffix}")
    if attr.get("id"):
        selectors.append(f"@{class_name}[id={_quoted(attr['id'])}]{suffix}")
    if attr.get("text") not in (None, ""):
        selectors.append(f"@{class_name}[text={_quoted(attr['text'])}]{suffix}")
    if attr.get("desc") not in (None, ""):
        selectors.append(f"@{class_name}[desc={_quoted(attr['desc'])}]{suffix}")
    if not selectors and semantic_child is not None and semantic_child.get("pid") == node.get("id"):
        child_attr = semantic_child.get("attr", {})
        child_class = str(child_attr.get("name") or "*").rsplit(".", 1)[-1]
        for field in ("vid", "id", "text", "desc"):
            value = child_attr.get(field)
            if value not in (None, ""):
                selectors.append(
                    f"@{class_name}[clickable=true][visibleToUser=true] > "
                    f"{child_class}[{field}={_quoted(value)}][visibleToUser=true]"
                )
                break
    return selectors


def summarize_node(node: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
    attr = node.get("attr", {})
    ancestors = []
    parent = by_id.get(node.get("pid"))
    while parent is not None:
        parent_attr = parent.get("attr", {})
        ancestors.append(
            {
                "node_id": parent.get("id"),
                "name": parent_attr.get("name"),
                "vid": parent_attr.get("vid"),
                "text": parent_attr.get("text"),
                "clickable": parent_attr.get("clickable"),
            }
        )
        parent = by_id.get(parent.get("pid"))
    action_node = nearest_clickable(node, by_id)
    return {
        "node_id": node.get("id"),
        "parent_id": node.get("pid"),
        "attr": attr,
        "action_node_id": action_node.get("id") if action_node else None,
        "action_node_attr": action_node.get("attr") if action_node else None,
        "selectors": selector_suggestions(
            action_node or node,
            semantic_child=node if action_node is not None and action_node.get("id") != node.get("id") else None,
        ),
        "ancestors": ancestors,
    }


def inspect(
    data: dict[str, Any],
    *,
    query: str | None = None,
    node_id: int | None = None,
    field: str = "auto",
    exact: bool = False,
    clickable_only: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    nodes = data["nodes"]
    by_id = {node["id"]: node for node in nodes}
    matches = []
    query_folded = query.casefold() if query is not None else None
    fields = SEARCH_FIELDS if field == "auto" else (field,)
    for node in nodes:
        attr = node.get("attr", {})
        if node_id is not None and node.get("id") != node_id:
            continue
        if clickable_only and not attr.get("clickable"):
            continue
        if query_folded is not None:
            values = [str(attr.get(name)) for name in fields if attr.get(name) is not None]
            if exact:
                found = any(value.casefold() == query_folded for value in values)
            else:
                found = any(query_folded in value.casefold() for value in values)
            if not found:
                continue
        matches.append(summarize_node(node, by_id))
        if len(matches) >= limit:
            break
    return {
        "snapshot": {
            "id": data.get("id"),
            "appId": data.get("appId"),
            "activityId": data.get("activityId"),
            "appVersion": (data.get("appInfo") or {}).get("versionName"),
            "screen": [data.get("screenWidth"), data.get("screenHeight")],
            "nodeCount": len(nodes),
        },
        "matchCount": len(matches),
        "matches": matches,
    }


def render_text(report: dict[str, Any]) -> str:
    meta = report["snapshot"]
    lines = [
        f"snapshot={meta['id']} app={meta['appId']} activity={meta['activityId']}",
        f"version={meta['appVersion']} screen={meta['screen']} nodes={meta['nodeCount']}",
        f"matches={report['matchCount']}",
    ]
    for item in report["matches"]:
        attr = item["attr"]
        lines.extend(
            [
                "",
                f"node={item['node_id']} action_node={item['action_node_id']}",
                f"class={attr.get('name')} id={attr.get('id')} vid={attr.get('vid')}",
                f"text={attr.get('text')!r} desc={attr.get('desc')!r}",
                f"clickable={attr.get('clickable')} visible={attr.get('visibleToUser')} "
                f"bounds=[{attr.get('left')},{attr.get('top')}][{attr.get('right')},{attr.get('bottom')}]",
            ]
        )
        action_attr = item.get("action_node_attr") or {}
        if item["action_node_id"] != item["node_id"]:
            lines.append(
                f"action_class={action_attr.get('name')} action_id={action_attr.get('id')} "
                f"action_vid={action_attr.get('vid')}"
            )
        for selector in item["selectors"]:
            lines.append(f"selector: {selector}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--query", help="Case-insensitive text/desc/vid/id/class search")
    target.add_argument("--node-id", type=int)
    parser.add_argument("--field", choices=("auto",) + SEARCH_FIELDS, default="auto")
    parser.add_argument("--exact", action="store_true")
    parser.add_argument("--clickable-only", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = inspect(
            load_snapshot(args.snapshot),
            query=args.query,
            node_id=args.node_id,
            field=args.field,
            exact=args.exact,
            clickable_only=args.clickable_only,
            limit=args.limit,
        )
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.as_json else render_text(report))
    return 0 if report["matchCount"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
