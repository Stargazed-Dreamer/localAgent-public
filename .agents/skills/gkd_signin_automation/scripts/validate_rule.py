#!/usr/bin/env python3
"""Validate a GKD subscription and check simple selectors against snapshots."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from inspect_snapshot import load_snapshot


HEAD_RE = re.compile(r"^@?(?P<class>[A-Za-z*][A-Za-z0-9_.$]*)")
ATTR_RE = re.compile(
    r"\[(?P<name>[A-Za-z][A-Za-z0-9_.]*)=(?P<value>\"(?:\\.|[^\"])*\"|true|false|null|-?\d+)\]"
)


def _normalize_value(raw: str) -> Any:
    if raw.startswith('"'):
        return json.loads(raw)
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    return int(raw)


def parse_simple_selector(selector: str) -> tuple[str, dict[str, Any]] | None:
    head = HEAD_RE.match(selector)
    if not head:
        return None
    attrs: dict[str, Any] = {}
    consumed = head.end()
    for match in ATTR_RE.finditer(selector, consumed):
        if selector[consumed : match.start()].strip():
            return None
        attrs[match.group("name")] = _normalize_value(match.group("value"))
        consumed = match.end()
    if selector[consumed:].strip():
        return None
    return head.group("class"), attrs


def selector_matches_node(parsed: tuple[str, dict[str, Any]], node: dict[str, Any]) -> bool:
    class_name, conditions = parsed
    attr = node.get("attr", {})
    actual_class = str(attr.get("name") or "")
    if class_name != "*" and class_name not in (actual_class, actual_class.rsplit(".", 1)[-1]):
        return False
    return all(attr.get(name) == value for name, value in conditions.items())


def selector_resource_key(selector: str) -> tuple[str, str] | None:
    parsed = parse_simple_selector(selector)
    if not parsed:
        return None
    class_name, attrs = parsed
    if isinstance(attrs.get("vid"), str):
        return class_name, attrs["vid"]
    full_id = attrs.get("id")
    if isinstance(full_id, str) and ":id/" in full_id:
        return class_name, full_id.rsplit(":id/", 1)[-1]
    return None


def _iter_groups(subscription: dict[str, Any]):
    for app in subscription.get("apps", []):
        for group in app.get("groups", []):
            yield "app", app.get("id"), group
    for group in subscription.get("globalGroups", []):
        yield "global", None, group


def validate_subscription(subscription: dict[str, Any], snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    if not isinstance(subscription.get("id"), int):
        errors.append("subscription.id must be an integer")
    if not isinstance(subscription.get("name"), str) or not subscription["name"].strip():
        errors.append("subscription.name must be a non-empty string")
    if not isinstance(subscription.get("version"), int) or subscription.get("version", 0) < 0:
        errors.append("subscription.version must be a non-negative integer")
    if not isinstance(subscription.get("apps"), list):
        errors.append("subscription.apps must be a list")

    app_ids: set[str] = set()
    for app in subscription.get("apps", []):
        app_id = app.get("id")
        if not isinstance(app_id, str) or not app_id:
            errors.append("every app requires a non-empty id")
        elif app_id in app_ids:
            errors.append(f"duplicate app id: {app_id}")
        else:
            app_ids.add(app_id)
        group_keys: set[Any] = set()
        for group in app.get("groups", []):
            key = group.get("key")
            if key in group_keys:
                errors.append(f"duplicate group key in {app_id}: {key}")
            group_keys.add(key)

    for scope, app_id, group in _iter_groups(subscription):
        location = f"{scope}:{app_id or '*'}:group:{group.get('key')}"
        maximum = group.get("actionMaximum")
        if maximum is not None and (not isinstance(maximum, int) or maximum <= 0):
            errors.append(f"{location} actionMaximum must be a positive integer")
        rules = group.get("rules")
        if not isinstance(rules, list) or not rules:
            errors.append(f"{location} requires a non-empty rules list")
            continue
        resource_keys: dict[tuple[str, str], str] = {}
        for rule_index, rule in enumerate(rules):
            selectors = rule.get("matches", [])
            if not selectors:
                warnings.append(f"{location}:rule:{rule_index} has no matches; verify intentional activity-only action")
            for selector in selectors:
                if not isinstance(selector, str) or not selector:
                    errors.append(f"{location}:rule:{rule_index} contains an invalid selector")
                    continue
                resource_key = selector_resource_key(selector)
                if resource_key and resource_key in resource_keys:
                    warnings.append(
                        f"{location} has equivalent resource selectors: "
                        f"{resource_keys[resource_key]!r} and {selector!r}"
                    )
                elif resource_key:
                    resource_keys[resource_key] = selector

                parsed = parse_simple_selector(selector)
                if not parsed:
                    checks.append({"selector": selector, "status": "unchecked_complex"})
                    continue
                relevant = [s for s in snapshots if not app_id or s.get("appId") == app_id]
                counts = [sum(selector_matches_node(parsed, node) for node in snap["nodes"]) for snap in relevant]
                status = "matched" if any(counts) else ("no_relevant_snapshot" if not relevant else "not_matched")
                checks.append(
                    {
                        "selector": selector,
                        "status": status,
                        "snapshotIds": [snap.get("id") for snap in relevant],
                        "matchCounts": counts,
                    }
                )
                if status == "not_matched":
                    warnings.append(f"{location} selector did not match provided snapshots: {selector}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "selectorChecks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subscription", type=Path)
    parser.add_argument("--snapshot", type=Path, action="append", default=[])
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        subscription = json.loads(args.subscription.read_text(encoding="utf-8"))
        snapshots = [load_snapshot(path) for path in args.snapshot]
        report = validate_subscription(subscription, snapshots)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
