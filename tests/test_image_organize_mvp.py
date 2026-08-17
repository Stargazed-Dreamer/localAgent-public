"""Focused tests for the image organizer's local-first MVP."""

import sqlite3

import pytest
from PIL import Image

from workspace.image_organize_remote_vl_sample.image_pipeline.policy import (
    RemoteVLPolicyError,
    ensure_move_source_is_workspace,
    inspect_remote_vl_source,
)
from workspace.image_organize_remote_vl_sample.image_pipeline.storage import (
    init_db,
    register_images,
    search_files,
)


def test_registers_hashes_duplicates_and_fts(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    image = Image.new("RGB", (24, 16), "red")
    image.save(source / "one.png")
    (source / "copy.png").write_bytes((source / "one.png").read_bytes())

    connection = init_db(tmp_path / "files.db")
    connection.row_factory = sqlite3.Row
    assert register_images(connection, source, sorted(source.rglob("*"))) == 2

    rows = connection.execute(
        "SELECT id, filename, sha256, duplicate_of FROM files ORDER BY id"
    ).fetchall()
    assert rows[0]["sha256"]
    assert rows[1]["duplicate_of"] == rows[0]["id"]
    assert search_files(connection, "one")[0]["filename"] == "one.png"
    connection.close()


def test_remote_policy_rejects_modelscope(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "vl_provider": "modelscope_dsv4_pro",
                "vl_model": "Qwen/Qwen3-VL-235B-A22B-Instruct",
                "vl_available": True,
            }

    monkeypatch.setattr(
        "workspace.image_organize_remote_vl_sample.image_pipeline.policy.requests.get",
        lambda *args, **kwargs: Response(),
    )
    with pytest.raises(RemoteVLPolicyError, match="provider="):
        inspect_remote_vl_source("http://127.0.0.1:8766")


def test_move_guard_rejects_external_source(tmp_path):
    with pytest.raises(RemoteVLPolicyError):
        ensure_move_source_is_workspace(tmp_path / "outside", tmp_path / "workspace")
