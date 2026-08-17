#!/usr/bin/env python3
"""Collect GKD snapshots and diagnostic files from an Android device over ADB."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GKD_ROOT = "/sdcard/Android/data/li.songe.gkd/files"


class AdbError(RuntimeError):
    pass


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise AdbError(f"Command failed: {' '.join(command)}\n{detail}")
    return result


def select_device(requested_serial: str | None = None) -> str:
    result = _run(["adb", "devices"])
    devices = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    if requested_serial:
        if requested_serial not in devices:
            raise AdbError(f"ADB device is not ready: {requested_serial}")
        return requested_serial
    if len(devices) != 1:
        raise AdbError(f"Expected exactly one ready ADB device, found {len(devices)}: {devices}")
    return devices[0]


def adb(serial: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["adb", "-s", serial, *args], check=check)


def remote_names(serial: str, remote_dir: str) -> list[str]:
    result = adb(serial, "shell", "ls", "-1", remote_dir, check=False)
    if result.returncode != 0:
        return []
    return [line.strip().rstrip("\r") for line in result.stdout.splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pull_file(
    serial: str,
    remote_path: str,
    local_path: Path,
    *,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    record: dict[str, Any] = {"remote": remote_path, "local": local_path.as_posix()}
    if local_path.exists() and not force:
        record.update(
            status="existing",
            size=local_path.stat().st_size,
            sha256=sha256_file(local_path),
        )
        return record
    if dry_run:
        record["status"] = "planned"
        return record
    local_path.parent.mkdir(parents=True, exist_ok=True)
    result = adb(serial, "pull", remote_path, str(local_path), check=False)
    if result.returncode != 0:
        record.update(status="failed", error=(result.stderr.strip() or result.stdout.strip()))
        return record
    record.update(status="pulled", size=local_path.stat().st_size, sha256=sha256_file(local_path))
    return record


def collect(args: argparse.Namespace) -> dict[str, Any]:
    serial = select_device(args.serial)
    output = args.output.resolve()
    records: list[dict[str, Any]] = []

    snapshot_ids = [name for name in remote_names(serial, f"{GKD_ROOT}/snapshot") if name.isdigit()]
    snapshot_ids.sort(key=int)
    if args.snapshots >= 0:
        snapshot_ids = snapshot_ids[-args.snapshots :] if args.snapshots else []
    for snapshot_id in snapshot_ids:
        for suffix in (".json", ".min.json", ".png"):
            name = f"{snapshot_id}{suffix}"
            records.append(
                pull_file(
                    serial,
                    f"{GKD_ROOT}/snapshot/{snapshot_id}/{name}",
                    output / "snapshots" / snapshot_id / name,
                    force=args.force,
                    dry_run=args.dry_run,
                )
            )

    if args.logs != 0:
        log_names = sorted(name for name in remote_names(serial, f"{GKD_ROOT}/log") if name.endswith(".log"))
        if args.logs > 0:
            log_names = log_names[-args.logs :]
        for name in log_names:
            records.append(
                pull_file(
                    serial,
                    f"{GKD_ROOT}/log/{name}",
                    output / "logs" / name,
                    force=args.force,
                    dry_run=args.dry_run,
                )
            )

    if args.subscriptions:
        for name in sorted(remote_names(serial, f"{GKD_ROOT}/subscription")):
            if name.endswith(".json"):
                records.append(
                    pull_file(
                        serial,
                        f"{GKD_ROOT}/subscription/{name}",
                        output / "subscriptions" / name,
                        force=args.force,
                        dry_run=args.dry_run,
                    )
                )

    if args.store:
        for name in ("action_count.txt", "store.json"):
            records.append(
                pull_file(
                    serial,
                    f"{GKD_ROOT}/store/{name}",
                    output / "store" / name,
                    force=args.force,
                    dry_run=args.dry_run,
                )
            )

    manifest = {
        "schema_version": 1,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "serial": serial,
        "gkd_root": GKD_ROOT,
        "dry_run": args.dry_run,
        "files": records,
    }
    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Destination directory")
    parser.add_argument("--serial", help="ADB serial; required only when multiple devices are connected")
    parser.add_argument("--snapshots", type=int, default=10, help="Latest snapshot directories; -1 means all")
    parser.add_argument("--logs", type=int, default=2, help="Latest logs; -1 means all, 0 disables logs")
    parser.add_argument("--no-subscriptions", dest="subscriptions", action="store_false")
    parser.add_argument("--no-store", dest="store", action="store_false")
    parser.add_argument("--force", action="store_true", help="Overwrite existing destination files")
    parser.add_argument("--dry-run", action="store_true", help="List planned transfers without writing")
    parser.set_defaults(subscriptions=True, store=True)
    return parser


def main() -> int:
    try:
        manifest = collect(build_parser().parse_args())
    except (AdbError, OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    counts: dict[str, int] = {}
    for item in manifest["files"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    print(json.dumps({"serial": manifest["serial"], "counts": counts}, ensure_ascii=False))
    return 0 if not counts.get("failed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
