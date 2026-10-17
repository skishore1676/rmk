"""Synthetic notebook and PDF acceptance; no tablet, model or cloud calls."""
import base64
import fcntl
import io
import json
from pathlib import Path
import stat
import uuid

from PIL import Image
from pypdf import PdfReader
import pytest

from rmk import capture as capture_module
from rmk.capture import _private_dir
from rmk.companion import Companion
from rmk.config import Config
from rmk.snapshot import digest

NB, P1, P2, P3, OTHER, FOLDER = [str(uuid.UUID(int=n)) for n in (100, 1, 2, 3, 200, 300)]


def render(data, output):
    tiles = []
    for index, (start, end) in enumerate(((0, 300), (150, 450), (300, 600), (450, 700)), 1):
        path = output / f"tile-{index:02d}.png"
        Image.new("RGB", (60, end - start), tuple(bytes.fromhex(digest(data)[:6]))).save(path)
        path.chmod(0o600)
        tiles.append({"file": path.name, "y_start": start, "y_end": end})
    return {"width": 60, "height": 700, "tiles": tiles}


def document(root, notebook=NB, pages=(P1, P2), name="Thoughts", **metadata):
    (root / notebook).mkdir(exist_ok=True)
    (root / f"{notebook}.metadata").write_text(json.dumps({"type": "DocumentType", "visibleName": name, **metadata}))
    (root / f"{notebook}.content").write_text(json.dumps({"fileType": "notebook", "pages": list(pages)}))
    for page in pages:
        (root / notebook / f"{page}.rm").write_bytes(page.encode())


@pytest.fixture
def service(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    document(root)
    monkeypatch.setattr(capture_module, "render_tiles", render)
    return Companion(config=Config(transport="local", root=str(root)), state_dir=tmp_path / "captures",
                     companion_dir=tmp_path / "posting", notebook_ids=[NB], runtime_file=tmp_path / "absent.json")


def call(service, name, **arguments):
    result = service.call_tool("remarkable_" + name, arguments)
    assert not result.get("isError"), result
    return result["structuredContent"]


def failure(service, name, **arguments):
    result = service.call_tool("remarkable_" + name, arguments)
    assert result["isError"], result
    return json.loads(result["content"][0]["text"])["error"]


def snapshot(service, **arguments):
    return call(service, "capture", notebook_id=NB, **arguments)


def ack(service, captured):
    return call(service, "acknowledge", notebook_id=NB, consumer=captured["consumer"],
                revision=captured["revision"], expected_previous_revision=captured["expected_previous_revision"])


def test_baseline_is_existing_context_and_read_does_not_acknowledge(service):
    root = Path(service._config.root)
    original = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = snapshot(service)
    assert result["changes"]["baseline"] and not result["changes"]["added"]
    assert result["expected_previous_revision"] is None and not result["checkpoint_advanced"]
    assert result["sync_freshness"] == "unknown"
    first = call(service, "read_page", notebook_id=NB, revision=result["revision"], page_id=P1)
    assert first["next_tile"] == 3 and not first["covers_entire_page"]
    last = call(service, "read_page", notebook_id=NB, revision=result["revision"], page_id=P1, tile_start=3)
    assert last["next_tile"] is None and last["tiles"][-1]["y_end"] == 700
    assert snapshot(service)["expected_previous_revision"] is None
    ack(service, result)
    assert not snapshot(service)["changes"]["baseline"]
    assert original == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert not list(service._state_dir.rglob("intake-state.json"))


def test_background_captures_cannot_consume_the_interactive_delta(service):
    initial = snapshot(service)
    ack(service, initial)
    root = Path(service._config.root)
    document(root, pages=(P1, P2, P3))
    store = service._store(NB)
    store.capture()  # independent background transition adds a page
    (root / NB / f"{P1}.rm").write_bytes(b"edited oldest page")
    store.capture()  # subsequent transition edits another page
    current = snapshot(service)
    assert current["changes"]["added"] == [P3]
    assert current["changes"]["edited"] == [P1]
    assert current["since_revision"] == initial["revision"]
    assert snapshot(service, consumer="another-client")["changes"]["baseline"]


def test_pinned_read_returns_historical_verified_pixels_even_after_change(service):
    first = snapshot(service)
    (Path(service._config.root) / NB / f"{P1}.rm").write_bytes(b"a newer page")
    current = snapshot(service)
    assert current["revision"] != first["revision"]
    result = service.call_tool("remarkable_read_page", {"notebook_id": NB, "revision": first["revision"], "page_id": P1})
    assert not result["isError"]
    image = result["content"][1]
    pixels = Image.open(io.BytesIO(base64.b64decode(image["data"])))
    assert pixels.getpixel((0, 0)) == tuple(bytes.fromhex(digest(P1.encode())[:6]))
    assert result["structuredContent"]["revision"] == first["revision"]
    assert failure(service, "read_page", notebook_id=NB, revision="f" * 64, page_id=P1) == "revision_unavailable"


def test_stale_acknowledgement_cannot_overwrite_newer_offer_or_checkpoint(service):
    first = snapshot(service)
    ack(service, first)
    page = Path(service._config.root) / NB / f"{P1}.rm"
    page.write_bytes(b"second")
    second = snapshot(service)
    page.write_bytes(b"third")
    third = snapshot(service)
    assert failure(service, "acknowledge", notebook_id=NB, revision=second["revision"],
                   expected_previous_revision=first["revision"]) == "checkpoint_conflict"
    ack(service, third)
    assert ack(service, third)["status"] == "already_acknowledged"
    assert failure(service, "acknowledge", notebook_id=NB, revision=second["revision"],
                   expected_previous_revision=first["revision"]) == "checkpoint_conflict"
    assert snapshot(service)["expected_previous_revision"] == third["revision"]


def test_content_reversion_does_not_allow_stale_acknowledgement(service):
    first = snapshot(service)
    ack(service, first)
    page = Path(service._config.root) / NB / f"{P1}.rm"
    page.write_bytes(b"second")
    second = snapshot(service)
    page.write_bytes(P1.encode())
    reverted = snapshot(service)
    assert reverted["revision"] == first["revision"]
    assert reverted["observed_at"] != reverted["artifact_created_at"]
    ack(service, reverted)
    assert failure(service, "acknowledge", notebook_id=NB, revision=second["revision"],
                   expected_previous_revision=first["revision"]) == "checkpoint_conflict"


def test_reader_checkpoint_survives_restart_and_is_private(service):
    first = snapshot(service)
    ack(service, first)
    new_service = Companion(config=service._config, state_dir=service._state_dir, notebook_ids=[NB], runtime_file=service._runtime_file)
    assert snapshot(new_service)["since_revision"] == first["revision"]
    path = service._reader_path(service._store(NB), "codex")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_capture_shares_runtime_lock_without_mutating_checkpoint(service):
    store = service._store(NB)
    _private_dir(store.directory)
    with (store.directory / ".refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert failure(service, "capture", notebook_id=NB) == "busy"
        assert not (store.directory / "state.json").exists()


def test_selection_applies_to_capture_and_historical_reads(service):
    root = Path(service._config.root)
    document(root, notebook=OTHER)
    assert failure(service, "capture", notebook_id=OTHER) == "notebook_not_selected"
    assert failure(service, "read_page", notebook_id=OTHER, revision="f" * 64, page_id=P1) == "notebook_not_selected"
    entries = call(service, "browse")["documents"]
    assert next(d for d in entries if d["id"] == OTHER)["selected"] is False
    service._notebook_ids = []
    assert failure(service, "capture", notebook_id=NB) == "notebook_not_selected"


def test_fresh_inventory_hides_ancestor_trash_and_reports_corruption(service):
    root = Path(service._config.root)
    assert call(service, "browse")["documents"][0]["name"] == "Thoughts"
    document(root, name="Renamed")
    assert call(service, "browse", query="Renamed")["documents"][0]["id"] == NB
    (root / f"{FOLDER}.metadata").write_text(json.dumps({"type": "CollectionType", "visibleName": "Old", "parent": "trash"}))
    document(root, parent=FOLDER)
    assert call(service, "browse")["documents"] == []
    assert failure(service, "capture", notebook_id=NB) == "notebook_unavailable"
    (root / f"{OTHER}.metadata").write_text("{")
    assert call(service, "browse")["inventory_complete"] is False


def test_imported_pdf_is_not_presented_as_a_complete_handwritten_notebook(service):
    (Path(service._config.root) / f"{NB}.content").write_text(json.dumps({"fileType": "pdf", "pages": [P1]}))
    assert call(service, "browse")["documents"][0]["readable"] is False
    assert failure(service, "capture", notebook_id=NB) == "notebook_unavailable"


def test_corrupt_or_symlinked_artifacts_fail_without_exposing_outside_bytes(service, tmp_path):
    first = snapshot(service)
    store = service._store(NB)
    tile = store.directory / "revisions" / first["revision"] / "pages" / P1 / "tile-01.png"
    original = tile.read_bytes()
    tile.write_bytes(b"corrupt")
    assert failure(service, "read_page", notebook_id=NB, revision=first["revision"], page_id=P1) == "capture_unavailable"
    outside = tmp_path / "outside.png"
    outside.write_bytes(original)
    tile.unlink()
    tile.symlink_to(outside)
    assert failure(service, "read_page", notebook_id=NB, revision=first["revision"], page_id=P1) == "invalid_revision"


@pytest.mark.parametrize("name,args", [
    ("capture", {"notebook_id": "../escape"}),
    ("capture", {"notebook_id": NB, "consumer": "../../escape"}),
    ("capture", {"notebook_id": NB, "extra": True}),
    ("browse", {"limit": True}),
    ("browse", {"limit": 0}),
    ("read_page", {"notebook_id": NB, "revision": "f" * 64, "page_id": P1, "tile_limit": 4}),
    ("acknowledge", {"notebook_id": NB, "revision": "f" * 64}),
    ("prepare_document", {"title": "Title", "body": "bad\u0000text"}),
])
def test_invalid_arguments_have_safe_errors(service, name, args):
    assert failure(service, name, **args) == "invalid_arguments"


def test_status_and_tool_discovery_work_without_desktop_setup(tmp_path):
    service = Companion(config=Config(), runtime_file=tmp_path / "absent", notebook_ids=[])
    assert len(service.tools) == 9
    result = call(service, "status")
    assert not result["source_available"]
    assert result["cloud_authentication"] == "not_checked"
    assert result["sync_freshness"] == "unknown"


def test_existing_runtime_selection_and_state_are_reused(service):
    runtime = service._runtime_file
    runtime.write_text(json.dumps({"schema": "rmk.runtime.v1", "notebook_id": NB, "state_dir": str(service._state_dir)}))
    service._notebook_ids = None
    service._state_dir = None
    assert service._store(NB).base == Path(json.loads(runtime.read_text())["state_dir"])
    assert call(service, "status")["selected_notebook_ids"] == [NB]


def test_capture_respects_existing_runtime_storage_limit(service):
    first = snapshot(service)
    service._runtime_file.write_text(json.dumps({"schema": "rmk.runtime.v1", "notebook_id": NB,
                                                "state_dir": str(service._state_dir), "max_storage_bytes": 1}))
    assert failure(service, "capture", notebook_id=NB) == "storage_limit"
    assert service._store(NB)._load()[0]["revision"] == first["revision"]


class Cloud:
    def __init__(self):
        self.calls = []

    def stat_document(self, name, parent):
        return None

    def upload_file(self, path, **kwargs):
        self.calls.append(Path(path).read_bytes())


def test_document_prepare_creates_exact_readable_pdf_preview_and_stable_id(service):
    service._cloud_client = Cloud()
    body = "# Decisions\nUse the existing rmk source.\n\n- Read all page tiles.\n- Treat <instructions> & links as source text."
    prepared = call(service, "prepare_document", title="Meeting brief", body=body)
    assert prepared["body"] == body and prepared["uploaded"] is False and prepared["page_count"] == 1
    repeated = call(service, "prepare_document", title="Meeting brief", body=body)
    assert repeated["prepared_id"] == prepared["prepared_id"]
    file = service._companion_dir / "prepared" / prepared["prepared_id"] / "Meeting brief.pdf"
    text = PdfReader(file).pages[0].extract_text()
    assert "<instructions> & links" in text
    assert not service._cloud_client.calls
    sent = call(service, "post_document", prepared_id=prepared["prepared_id"], request_id="meeting-brief-1")
    assert sent["status"] == "submitted" and sent["tablet_sync"] == "unknown"
    assert service._cloud_client.calls == [file.read_bytes()]
    assert call(service, "post_status", request_id="meeting-brief-1") == sent
    assert call(service, "post_document", prepared_id=prepared["prepared_id"], request_id="meeting-brief-1") == sent
    assert len(service._cloud_client.calls) == 1


def test_upload_requires_explicit_host_root_and_refuses_symlink_escape(service, tmp_path):
    source = tmp_path / "private.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    assert failure(service, "prepare_upload", file_path=str(source)) == "upload_root_not_configured"
    root = tmp_path / "allowed"
    root.mkdir()
    service._upload_root = root
    assert failure(service, "prepare_upload", file_path=str(source)) == "file_not_allowed"
    (root / "linked.pdf").symlink_to(source)
    assert failure(service, "prepare_upload", file_path=str(root / "linked.pdf")) == "invalid_file"
    (root / "selected.pdf").write_bytes(source.read_bytes())
    assert call(service, "prepare_upload", file_path=str(root / "selected.pdf"))["media_type"] == "application/pdf"
