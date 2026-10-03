"""Synthetic-only capture acceptance; no model or private source fixtures."""
import json
import multiprocessing
import os
from pathlib import Path
import stat
import uuid

import pytest
from typer.testing import CliRunner

from rmk import capture as module
from rmk.capture import CaptureError, CaptureStore
from rmk.snapshot import SnapshotError, active_pages, read_snapshot
from rmk.transport import LocalTransport

NB = str(uuid.UUID(int=100))
P1, P2, P3 = [str(uuid.UUID(int=n)) for n in (1, 2, 3)]


def fake_render(data, output):
    file = output / "tile-01.png"
    file.write_bytes(b"synthetic-render:" + data)
    file.chmod(0o600)
    return {"width": 100, "height": 100,
            "tiles": [{"file": file.name, "y_start": 0, "y_end": 100}]}


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / NB).mkdir()
    (root / f"{NB}.metadata").write_text(json.dumps({
        "visibleName": "Thoughts fixture", "type": "DocumentType", "lastModified": "123"}))
    set_order(root, [P1, P2])
    for pid in (P1, P2):
        (root / NB / f"{pid}.rm").write_bytes(pid.encode())
    monkeypatch.setattr(module, "render_tiles", fake_render)
    return CaptureStore(tmp_path / "private", LocalTransport(), str(root), NB)


def set_order(root, ids):
    (root / f"{NB}.content").write_text(json.dumps({"fileType": "notebook", "pages": ids}))


def pointer(store):
    return (store.directory / "state.json").read_bytes()


def source_bytes(store):
    return {str(p): p.read_bytes() for p in Path(store.root).rglob("*") if p.is_file()}


def test_baseline_unchanged_and_source_preservation(store):
    before = source_bytes(store)
    result = store.capture()
    assert result["changes"] == {"baseline": True, "added": [], "edited": [], "removed": [],
                                  "reordered": False, "renamed": False}
    first = pointer(store)
    replay = store.capture()
    assert replay["revision"] == result["revision"] and replay["unchanged"]
    assert pointer(store) == first
    assert replay["changes"]["baseline"] is False
    assert before == source_bytes(store)
    assert len(list((store.directory / "revisions").iterdir())) == 1


def test_new_page_and_edit_older_page(store):
    store.capture()
    root = Path(store.root)
    set_order(root, [P1, P2, P3])
    (root / NB / f"{P3}.rm").write_bytes(b"new page")
    (root / NB / f"{P1}.rm").write_bytes(b"edit oldest page")
    delta = store.capture()["changes"]
    assert delta["added"] == [P3] and delta["edited"] == [P1]
    assert not delta["reordered"] and not delta["baseline"]
    (root / NB / f"{P3}.rm").write_bytes(b"edit latest page")
    assert store.capture()["changes"]["edited"] == [P3]


def test_rename_reorder_deleted_orphans_and_empty_notebook(store):
    store.capture()
    root = Path(store.root)
    meta = root / f"{NB}.metadata"
    data = json.loads(meta.read_text()); data["visibleName"] = "Renamed"; meta.write_text(json.dumps(data))
    set_order(root, [P2, P1])
    delta = store.capture()["changes"]
    assert delta["renamed"] and delta["reordered"]
    assert delta["added"] == delta["edited"] == []
    (root / f"{NB}.content").write_text(json.dumps({"fileType": "notebook", "pages": [P1, P3],
        "cPages": {"pages": [{"id": P2, "deleted": {"value": False}},
                               {"id": P1, "deleted": {"value": True}}]}}))
    (root / NB / f"{P3}.rm").write_bytes(b"orphan, never read")
    delta = store.capture()["changes"]
    assert delta["removed"] == [P1] and not delta["reordered"]
    assert store.status()["page_count"] == 1
    set_order(root, [])
    assert store.capture()["changes"]["removed"] == [P2]
    assert store.status()["page_count"] == 0


@pytest.mark.parametrize("bad", [None, {}, {"pages": "bad"}, {"pages": [P1, P1]},
    {"pages": ["../escape"]}, {"pages": [1]}, {"pages": [P1], "cPages": {}},
    {"cPages": {"pages": [1]}}, {"cPages": {"pages": [{"id": P1, "deleted": {}}]}}])
def test_invalid_page_order(bad):
    with pytest.raises(SnapshotError):
        active_pages(bad)


def test_missing_page_no_checkpoint_advance(store):
    store.capture(); first = pointer(store)
    (Path(store.root) / NB / f"{P1}.rm").unlink()
    with pytest.raises(FileNotFoundError):
        store.capture()
    assert pointer(store) == first


@pytest.mark.parametrize("metadata", [{"deleted": True}, {"parent": "trash"}, {"type": "CollectionType"}])
def test_deleted_or_folder_notebook_fails_without_state(store, metadata):
    (Path(store.root) / f"{NB}.metadata").write_text(json.dumps(metadata))
    with pytest.raises(SnapshotError):
        store.capture()
    assert not (store.directory / "state.json").exists()


@pytest.mark.parametrize("content", ['{', '{"fileType":"pdf","pages":[]}', '[]'])
def test_unsupported_or_corrupt_content_no_checkpoint(store, content):
    (Path(store.root) / f"{NB}.content").write_text(content)
    with pytest.raises(SnapshotError):
        store.capture()
    assert not (store.directory / "state.json").exists()


def test_concurrent_source_change_during_render(store, monkeypatch):
    store.capture(); first = pointer(store)
    file = Path(store.root) / NB / f"{P1}.rm"
    file.write_bytes(b"first edit")
    def render(data, output):
        result = fake_render(data, output)
        file.write_bytes(b"sync during render")
        return result
    monkeypatch.setattr(module, "render_tiles", render)
    with pytest.raises(SnapshotError, match="changed"):
        store.capture()
    assert pointer(store) == first
    assert list((store.directory / "revisions").glob(".incomplete-*"))


def test_source_change_during_initial_read(store, monkeypatch):
    transport = store.transport
    original = transport.read_bytes
    count = 0
    def read(path):
        nonlocal count
        data = original(path); count += 1
        if count == 3:
            file = Path(store.root) / f"{NB}.metadata"
            file.write_bytes(file.read_bytes() + b" ")
        return data
    monkeypatch.setattr(transport, "read_bytes", read)
    with pytest.raises(SnapshotError, match="changed"):
        store.capture()
    assert not (store.directory / "state.json").exists()


def test_render_failure_no_checkpoint_and_safe_retry(store, monkeypatch):
    def fail(*args):
        raise RuntimeError("synthetic renderer failed")
    monkeypatch.setattr(module, "render_tiles", fail)
    with pytest.raises(RuntimeError):
        store.capture()
    assert not (store.directory / "state.json").exists()
    assert list((store.directory / "revisions").glob(".incomplete-*"))
    monkeypatch.setattr(module, "render_tiles", fake_render)
    assert store.capture()["changes"]["baseline"]


def test_interrupted_checkpoint_write_preserves_prior_and_reuses_artifacts(store, monkeypatch):
    store.capture(); first = pointer(store)
    (Path(store.root) / NB / f"{P1}.rm").write_bytes(b"edit")
    original = module.os.replace
    def fail(*args):
        raise OSError("simulated interrupted replace")
    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError):
        store.capture()
    assert pointer(store) == first
    assert list(store.directory.glob(".checkpoint-*"))
    monkeypatch.setattr(module.os, "replace", original)
    def no_render(*args):
        pytest.fail("completed immutable revision should be reused")
    monkeypatch.setattr(module, "render_tiles", no_render)
    assert store.capture()["changes"]["edited"] == [P1]


@pytest.mark.parametrize("bad", ['{', '{}', '[]', '{"schema":"wrong"}'])
def test_corrupt_state_fails_closed(store, bad):
    store.capture()
    state = store.directory / "state.json"; state.write_text(bad)
    with pytest.raises(CaptureError, match="Corrupt"):
        store.capture()
    assert state.read_text() == bad


def test_corrupt_artifact_fails_closed(store):
    result = store.capture(); first = pointer(store)
    tile = next(Path(result["artifact"]).rglob("*.png")); tile.write_bytes(b"corrupt")
    with pytest.raises(CaptureError):
        store.capture()
    assert pointer(store) == first


def _overlapping_child(state_dir, root, conn):
    candidate = CaptureStore(Path(state_dir), LocalTransport(), root, NB)
    try:
        candidate.capture()
    except CaptureError as exc:
        conn.send(str(exc))
    finally:
        conn.close()


def test_overlapping_process_and_lock_release(store):
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    with store._lock():
        process = ctx.Process(target=_overlapping_child, args=(str(store.base), store.root, child))
        process.start()
        assert parent.poll(5)
        assert "Another capture" in parent.recv()
        process.join(5)
        assert process.exitcode == 0
    assert store.capture()["changes"]["baseline"]


def test_more_than_twenty_pages_and_private_permissions(store):
    root = Path(store.root)
    ids = [str(uuid.UUID(int=n)) for n in range(1, 26)]
    set_order(root, ids)
    for pid in ids:
        (root / NB / f"{pid}.rm").write_bytes(pid.encode())
    result = store.capture()
    assert store.status()["page_count"] == 25
    manifest = json.loads((Path(result["artifact"]) / "manifest.json").read_text())
    assert manifest["active_order"] == ids and len(manifest["pages"]) == 25
    for path in [store.base, *store.base.rglob("*")]:
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected


def test_output_guard_before_any_source_read(store, tmp_path):
    root = Path(store.root)
    for dest in [root, root / "private", root.parent]:
        with pytest.raises(CaptureError, match="separate"):
            CaptureStore(dest, LocalTransport(), str(root), NB)
    repo = tmp_path / "repo"; repo.mkdir(); (repo / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(CaptureError, match="outside Git"):
        CaptureStore(repo / "raw", LocalTransport(), str(root), NB)


def test_cli_capture_status_changes_without_model(store, monkeypatch):
    from rmk.cli import app, Config
    from rmk.config import Config as ConfigType
    monkeypatch.setattr(Config, "load", lambda: ConfigType(transport="local", root=store.root))
    runner = CliRunner()
    for command in ["status", "capture", "capture", "status", "changes"]:
        result = runner.invoke(app, [command, NB, "--state-dir", str(store.base)])
        assert result.exit_code == 0, result.output
        json.loads(result.output)
    assert json.loads(result.output)["baseline"] is True


def test_observed_desktop_integer_tombstones():
    assert active_pages({"cPages": {"pages": [
        {"id": P1, "deleted": {"timestamp": "1:1", "value": 0}},
        {"id": P2, "deleted": {"timestamp": "1:3", "value": 1}},
    ]}}) == [P1]
    with pytest.raises(SnapshotError):
        active_pages({"cPages": {"pages": [{"id": P1, "deleted": {"value": 2}}]}})


def _crash_at_checkpoint(state_dir, root):
    def crash(*args):
        os._exit(7)
    module._atomic_write = crash
    candidate = CaptureStore(Path(state_dir), LocalTransport(), root, NB)
    candidate.capture()


def test_actual_process_exit_releases_lock_without_advancing(store):
    store.capture(); first = pointer(store)
    (Path(store.root) / NB / f"{P1}.rm").write_bytes(b"edit before crash")
    ctx = multiprocessing.get_context("fork")
    process = ctx.Process(target=_crash_at_checkpoint, args=(str(store.base), store.root))
    process.start(); process.join(5)
    assert process.exitcode == 7
    assert pointer(store) == first
    assert store.capture()["changes"]["edited"] == [P1]


def test_return_to_prior_revision_reuses_artifact_with_current_delta(store, monkeypatch):
    first = store.capture()
    file = Path(store.root) / NB / f"{P1}.rm"; original = file.read_bytes()
    file.write_bytes(b"edit"); store.capture()
    file.write_bytes(original)
    monkeypatch.setattr(module, "render_tiles", lambda *a: pytest.fail("must reuse prior revision"))
    result = store.capture()
    assert result["revision"] == first["revision"] and result["changes"]["edited"] == [P1]
    assert not result["changes"]["baseline"]


def test_status_does_not_read_source_and_unchanged_replay_retains_last_delta(store, monkeypatch):
    store.capture()
    monkeypatch.setattr(store.transport, "read_bytes", lambda *a: pytest.fail("status must not read source"))
    assert store.status()["capture"]["changes"]["baseline"]


def test_capture_cli_rejects_ssh_before_connecting(store, monkeypatch):
    from rmk import cli
    from rmk.config import Config
    monkeypatch.setattr(cli.Config, "load", lambda: Config(transport="ssh"))
    monkeypatch.setattr(cli, "SSHTransport", lambda **k: pytest.fail("must not authenticate"))
    result = CliRunner().invoke(cli.app, ["capture", NB, "--state-dir", str(store.base)])
    assert result.exit_code == 1 and "local desktop store" in result.output


@pytest.mark.parametrize("kind", ["empty_tiles", "missing_bottom", "missing_tile"])
def test_incomplete_render_never_commits(store, monkeypatch, kind):
    def bad_render(data, output):
        result = fake_render(data, output)
        if kind == "empty_tiles":
            result["tiles"] = []
        elif kind == "missing_bottom":
            result["height"] += 1
        else:
            (output / "tile-01.png").unlink()
        return result
    monkeypatch.setattr(module, "render_tiles", bad_render)
    with pytest.raises(CaptureError):
        store.capture()
    assert not (store.directory / "state.json").exists()
