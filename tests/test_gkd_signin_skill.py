from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "gkd_signin_automation"
SCRIPTS = SKILL_DIR / "scripts"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


inspect_snapshot = _load_module("inspect_snapshot", "inspect_snapshot.py")
validate_rule = _load_module("validate_rule", "validate_rule.py")


def _snapshot() -> dict:
    return {
        "id": 123,
        "appId": "com.example.app",
        "activityId": "com.example.app.MainActivity",
        "screenWidth": 1200,
        "screenHeight": 2664,
        "appInfo": {"versionName": "1.2.3"},
        "nodes": [
            {
                "id": 0,
                "pid": -1,
                "attr": {
                    "name": "android.widget.FrameLayout",
                    "visibleToUser": True,
                    "clickable": False,
                },
            },
            {
                "id": 1,
                "pid": 0,
                "attr": {
                    "id": "com.example.app:id/claim",
                    "vid": "claim",
                    "name": "android.view.ViewGroup",
                    "visibleToUser": True,
                    "clickable": True,
                    "left": 800,
                    "top": 300,
                    "right": 1100,
                    "bottom": 450,
                },
            },
            {
                "id": 2,
                "pid": 1,
                "attr": {
                    "name": "android.widget.TextView",
                    "text": "立即领",
                    "visibleToUser": True,
                    "clickable": False,
                    "left": 850,
                    "top": 330,
                    "right": 1000,
                    "bottom": 410,
                },
            },
        ],
    }


def test_inspect_reports_nearest_clickable_ancestor() -> None:
    report = inspect_snapshot.inspect(_snapshot(), query="立即领", exact=True)
    assert report["matchCount"] == 1
    match = report["matches"][0]
    assert match["node_id"] == 2
    assert match["action_node_id"] == 1
    assert match["selectors"][0] == '@ViewGroup[vid="claim"][clickable=true][visibleToUser=true]'


def test_inspect_anchors_semantic_child_when_clickable_parent_has_no_identity() -> None:
    snapshot = _snapshot()
    snapshot["nodes"][1]["attr"].pop("id")
    snapshot["nodes"][1]["attr"].pop("vid")
    report = inspect_snapshot.inspect(snapshot, query="立即领", exact=True)
    assert report["matches"][0]["selectors"] == [
        '@ViewGroup[clickable=true][visibleToUser=true] > '
        'TextView[text="立即领"][visibleToUser=true]'
    ]


def test_load_snapshot_from_zip_ignores_empty_min_json(tmp_path: Path) -> None:
    archive_path = tmp_path / "snapshot.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("123.min.json", json.dumps({**_snapshot(), "nodes": []}))
        archive.writestr("123.json", json.dumps(_snapshot()))
    loaded = inspect_snapshot.load_snapshot(archive_path)
    assert len(loaded["nodes"]) == 3


def test_validator_warns_about_equivalent_id_and_vid_rules() -> None:
    subscription = {
        "id": 7,
        "name": "test",
        "version": 1,
        "globalGroups": [],
        "apps": [
            {
                "id": "com.example.app",
                "groups": [
                    {
                        "key": 0,
                        "actionMaximum": 1,
                        "rules": [
                            {"matches": ['@ViewGroup[id="com.example.app:id/claim"]']},
                            {"matches": ['@ViewGroup[vid="claim"]']},
                        ],
                    }
                ],
            }
        ],
    }
    report = validate_rule.validate_subscription(subscription, [_snapshot()])
    assert report["valid"] is True
    assert any("equivalent resource selectors" in warning for warning in report["warnings"])
    assert all(check["status"] == "matched" for check in report["selectorChecks"])


def test_current_skland_v2_subscription_is_structurally_valid() -> None:
    path = ROOT / "workspace" / "gkd_signin" / "rules" / "skland_check_subscription.json"
    subscription = json.loads(path.read_text(encoding="utf-8"))
    report = validate_rule.validate_subscription(subscription, [])
    assert report["valid"] is True
    assert not any("equivalent resource selectors" in warning for warning in report["warnings"])


def test_skill_evals_json_is_valid() -> None:
    path = SKILL_DIR / "evals" / "evals.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["skill_name"] == "gkd-signin-automation"
    assert len(data["evals"]) == 4
