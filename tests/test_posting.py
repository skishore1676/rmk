"""No-network acceptance for private, durable new-document posting."""

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rmk.cloud import CloudError
from rmk.posting import PostingError, PostingStore


PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"
DOC_ID = "12345678-1234-1234-1234-123456789abc"


class FakeClient:
    def __init__(self, *, before=None, after="match", upload_error=None, before_hook=None, upload_hook=None):
        self.before = before
        self.after = after
        self.upload_error = upload_error
        self.before_hook = before_hook
        self.upload_hook = upload_hook
        self.checks = []
        self.uploads = []

    def stat_document(self, name, parent):
        self.checks.append((name, parent))
        if len(self.checks) == 1:
            if self.before_hook:
                self.before_hook()
            answer = self.before
        else:
            answer = self.after
        if isinstance(answer, BaseException):
            raise answer
        if answer == "match":
            return {"id": DOC_ID, "name": name, "cloud_path": f"{parent}/{name}", "type": "DocumentType", "verification": "exact_path_metadata"}
        return answer

    def upload_file(self, path, name=None, parent=""):
        self.uploads.append({"content": Path(path).read_bytes(), "name": name, "parent": parent})
        if self.upload_hook:
            self.upload_hook()
        if self.upload_error:
            raise self.upload_error
        # Deliberately not a verification signal: PostingStore must ignore it.
        return {"name": name, "path": "private-host-path", "token": "provider-secret"}


def fixture_store(tmp_path, client=None, *, max_bytes=50 * 1024 * 1024):
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    client = client or FakeClient()
    store = PostingStore(tmp_path / "posting", client, max_bytes=max_bytes)
    return source, client, store


def prepared_path(store, prepared):
    suffix = ".pdf" if prepared["media_type"] == "application/pdf" else ".epub"
    return store.prepared_dir / prepared["prepared_id"] / f"{prepared['name']}{suffix}"


def test_prepare_snapshots_private_immutable_bytes_without_network(tmp_path):
    source, client, store = fixture_store(tmp_path)
    prepared = store.prepare_file(source, name="Meeting brief", parent="/Personal")
    assert prepared["name"] == "Meeting brief"
    assert prepared["cloud_path"] == "/Personal/Meeting brief"
    assert prepared["bytes"] == len(PDF)
    assert len(prepared["sha256"]) == 64
    assert len(prepared["prepared_id"]) == 64
    assert not client.uploads and not client.checks
    assert str(tmp_path) not in json.dumps(prepared)
    staged = prepared_path(store, prepared)
    assert staged.read_bytes() == PDF
    assert stat.S_IMODE(staged.stat().st_mode) == 0o400
    assert stat.S_IMODE(staged.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((staged.parent / "metadata.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(store.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.state_dir / "posting.lock").stat().st_mode) == 0o600


def test_identical_preparation_is_stable_and_root_is_normalized(tmp_path):
    source, _, store = fixture_store(tmp_path)
    first = store.prepare_file(source, parent="/")
    second = store.prepare_file(source, parent="")
    assert first == second
    assert store.describe(first["prepared_id"]) == first
    renamed = store.prepare_file(source, name="Other name")
    assert renamed["sha256"] == first["sha256"]
    assert renamed["prepared_id"] != first["prepared_id"]


def test_post_persists_before_any_network_and_confirms_cloud_only(tmp_path):
    source, client, store = fixture_store(tmp_path)
    prepared = store.prepare_file(source)

    def before_network():
        receipt = store.status("request-1")
        assert receipt["status"] == "pending"
        assert receipt["stage"] == "checking_destination"
        assert not receipt["upload_attempted"]
        files = list(store.receipts_dir.glob("*.json"))
        assert len(files) == 1
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600

    def before_upload():
        receipt = store.status("request-1")
        assert receipt["stage"] == "uploading"
        assert receipt["upload_attempted"]

    client.before_hook = before_network
    client.upload_hook = before_upload
    source.write_bytes(PDF.replace(b"1.4", b"1.7"))
    receipt = store.post(prepared["prepared_id"], "request-1")
    assert receipt["status"] == "cloud_confirmed"
    assert receipt["cloud_document_id"] == DOC_ID
    assert receipt["tablet_sync"] == "unknown"
    assert receipt["cloud_content_verified"] is False
    assert client.uploads == [{"content": PDF, "name": "source", "parent": ""}]
    assert len(client.checks) == 2
    assert "provider-secret" not in json.dumps(receipt)
    assert "private-host-path" not in json.dumps(receipt)
    assert store.status("request-1") == receipt


def test_completed_receipt_survives_restart_and_retries_do_not_reupload(tmp_path):
    source, _, store = fixture_store(tmp_path)
    prepared = store.prepare_file(source)
    receipt = store.post(prepared["prepared_id"], "request-1")
    new_client = FakeClient()
    reloaded = PostingStore(store.state_dir, new_client)
    assert reloaded.post(prepared["prepared_id"], "request-1") == receipt
    assert reloaded.status("request-1") == receipt
    assert new_client.checks == [] and new_client.uploads == []


def test_uncertain_failure_is_saved_sanitized_and_never_retried(tmp_path):
    client = FakeClient(upload_error=CloudError("Authorization: Bearer SECRET timeout"))
    source, client, store = fixture_store(tmp_path, client)
    prepared = store.prepare_file(source)
    receipt = store.post(prepared["prepared_id"], "request-1")
    assert receipt["status"] == "unknown"
    assert receipt["reason"] == "upload_result_uncertain"
    assert "SECRET" not in json.dumps(receipt)
    assert len(client.uploads) == 1
    assert store.post(prepared["prepared_id"], "request-1") == receipt
    assert len(client.uploads) == 1
    with pytest.raises(PostingError, match="already has a posting request"):
        store.post(prepared["prepared_id"], "request-2")
    assert len(client.uploads) == 1


def test_process_interruption_retains_pending_receipt_and_blocks_second_upload(tmp_path):
    client = FakeClient(upload_error=KeyboardInterrupt())
    source, client, store = fixture_store(tmp_path, client)
    prepared = store.prepare_file(source)
    with pytest.raises(KeyboardInterrupt):
        store.post(prepared["prepared_id"], "request-1")
    pending = store.status("request-1")
    assert pending["status"] == "pending"
    assert pending["stage"] == "uploading"
    reloaded = PostingStore(store.state_dir, FakeClient())
    assert reloaded.post(prepared["prepared_id"], "request-1") == pending
    assert not reloaded.client.uploads and not reloaded.client.checks


@pytest.mark.parametrize("preflight,reason", [("match", "destination_already_exists"), (CloudError("token SECRET"), "destination_check_unavailable")])
def test_preflight_blocks_writes_when_destination_exists_or_cannot_be_checked(tmp_path, preflight, reason):
    source, client, store = fixture_store(tmp_path, FakeClient(before=preflight))
    prepared = store.prepare_file(source)
    receipt = store.post(prepared["prepared_id"], "request-1")
    assert receipt["status"] == "blocked"
    assert receipt["reason"] == reason
    assert receipt["upload_attempted"] is False
    assert "SECRET" not in json.dumps(receipt)
    assert not client.uploads


@pytest.mark.parametrize("readback", [None, CloudError("token SECRET"), {"name": "source"}, {"id": DOC_ID, "name": "source", "type": "DocumentType", "cloud_path": "/wrong/source", "verification": "exact_path_metadata"}])
def test_successful_command_without_exact_readback_is_only_submitted(tmp_path, readback):
    source, client, store = fixture_store(tmp_path, FakeClient(after=readback))
    prepared = store.prepare_file(source)
    receipt = store.post(prepared["prepared_id"], "request-1")
    assert receipt["status"] == "submitted"
    assert receipt["tablet_sync"] == "unknown"
    assert "cloud_document_id" not in receipt
    assert "SECRET" not in json.dumps(receipt)
    assert store.post(prepared["prepared_id"], "request-1") == receipt
    assert len(client.uploads) == 1


def test_request_cannot_be_reused_for_another_preparation(tmp_path):
    source, client, store = fixture_store(tmp_path)
    first = store.prepare_file(source)
    second = store.prepare_file(source, name="Different")
    store.post(first["prepared_id"], "request-1")
    with pytest.raises(PostingError, match="different preparation"):
        store.post(second["prepared_id"], "request-1")
    assert len(client.uploads) == 1


def test_concurrent_same_request_has_only_one_upload(tmp_path):
    source, client, store = fixture_store(tmp_path)
    prepared = store.prepare_file(source)
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(executor.map(lambda _: store.post(prepared["prepared_id"], "request-1"), range(8)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert len(client.uploads) == 1


def test_changed_staged_bytes_prevent_network_activity(tmp_path):
    source, client, store = fixture_store(tmp_path)
    prepared = store.prepare_file(source)
    staged = prepared_path(store, prepared)
    staged.chmod(0o600)
    staged.write_bytes(PDF.replace(b"1.4", b"1.7"))
    with pytest.raises(PostingError, match="has changed"):
        store.post(prepared["prepared_id"], "request-1")
    assert not client.checks and not client.uploads


@pytest.mark.parametrize("name", ["", "../escape", "/escape", "a/b", "a\\b", "--force", "-h", "~home", ".", "..", " A", "A ", "A\nB", "A\x00B", "A\u202eB", "A*", "A?", "A[B]", "x" * 181])
def test_invalid_document_names_never_stage_or_upload(tmp_path, name):
    source, client, store = fixture_store(tmp_path)
    with pytest.raises(PostingError):
        store.prepare_file(source, name=name)
    assert not list(store.prepared_dir.iterdir())
    assert not client.checks and not client.uploads


@pytest.mark.parametrize("parent", ["Personal", "--force", "../Personal", "/../Personal", "/A/../B", "//A", "/A//B", "/A/", "/A*", "/A\nB", "/~A"])
def test_invalid_parent_paths_are_rejected(tmp_path, parent):
    source, client, store = fixture_store(tmp_path)
    with pytest.raises(PostingError):
        store.prepare_file(source, parent=parent)
    assert not client.checks and not client.uploads


@pytest.mark.parametrize("suffix,content", [(".pdf", b"not a PDF"), (".pdf", b""), (".pdf", b"%PDF-1.4 missing footer"), (".epub", b"not a ZIP"), (".txt", PDF)])
def test_file_extension_magic_and_empty_file_validation(tmp_path, suffix, content):
    _, client, store = fixture_store(tmp_path)
    source = tmp_path / f"bad{suffix}"
    source.write_bytes(content)
    with pytest.raises(PostingError):
        store.prepare_file(source)
    assert not client.checks and not client.uploads


def test_size_limit_rejects_before_staging(tmp_path):
    source, client, store = fixture_store(tmp_path, max_bytes=len(PDF) - 1)
    with pytest.raises(PostingError, match="size limit"):
        store.prepare_file(source)
    assert not list(store.prepared_dir.iterdir())


def test_pdf_uppercase_extension_is_staged_lowercase(tmp_path):
    _, _, store = fixture_store(tmp_path)
    source = tmp_path / "Brief.PDF"
    source.write_bytes(PDF)
    prepared = store.prepare_file(source)
    assert prepared_path(store, prepared).name == "Brief.pdf"


def test_valid_epub_can_be_prepared_without_extracting_members(tmp_path):
    import zipfile

    _, client, store = fixture_store(tmp_path)
    source = tmp_path / "Book.epub"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", "<container/>")
    prepared = store.prepare_file(source)
    assert prepared["media_type"] == "application/epub+zip"
    assert prepared_path(store, prepared).read_bytes() == source.read_bytes()
    assert not client.checks and not client.uploads


def test_epub_archive_traversal_is_rejected(tmp_path):
    import zipfile

    _, _, store = fixture_store(tmp_path)
    source = tmp_path / "Book.epub"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", "<container/>")
        archive.writestr("../../escape", "bad")
    with pytest.raises(PostingError, match="EPUB"):
        store.prepare_file(source)
    assert not (tmp_path.parent / "escape").exists()


def test_source_symlink_and_symlinked_parent_are_rejected(tmp_path):
    source, client, store = fixture_store(tmp_path)
    link = tmp_path / "linked.pdf"
    link.symlink_to(source)
    with pytest.raises(PostingError, match="symbolic link"):
        store.prepare_file(link)
    link_dir = tmp_path / "linked-directory"
    link_dir.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PostingError, match="symbolic link"):
        store.prepare_file(link_dir / "source.pdf")
    assert not client.checks and not client.uploads


def test_nonregular_source_file_is_rejected_without_hanging(tmp_path):
    _, _, store = fixture_store(tmp_path)
    fifo = tmp_path / "pipe.pdf"
    os.mkfifo(fifo)
    with pytest.raises(PostingError, match="regular file"):
        store.prepare_file(fifo)


def test_symlinked_storage_and_receipt_are_not_followed(tmp_path):
    source, client, store = fixture_store(tmp_path)
    symlink = tmp_path / "state-link"
    symlink.symlink_to(store.state_dir, target_is_directory=True)
    with pytest.raises(PostingError, match="symbolic links"):
        PostingStore(symlink, client)
    with pytest.raises(PostingError, match="symbolic links"):
        PostingStore(symlink / "must-not-create", client)
    assert not (store.state_dir / "must-not-create").exists()
    request_path = store._receipt_path("request-1")
    request_path.symlink_to(source)
    with pytest.raises(PostingError, match="unreadable"):
        store.status("request-1")


@pytest.mark.parametrize("request_id", ["", "../escape", "-force", "a/b", "a\nsecret", "x" * 129])
def test_request_ids_cannot_address_arbitrary_paths(tmp_path, request_id):
    source, client, store = fixture_store(tmp_path)
    prepared = store.prepare_file(source)
    with pytest.raises(PostingError):
        store.post(prepared["prepared_id"], request_id)
    with pytest.raises(PostingError):
        store.status(request_id)
    assert not client.checks and not client.uploads


def test_missing_receipt_and_invalid_preparation_are_clear_errors(tmp_path):
    _, _, store = fixture_store(tmp_path)
    with pytest.raises(PostingError) as missing:
        store.status("request-1")
    assert missing.value.code == "request_not_found"
    with pytest.raises(PostingError) as invalid:
        store.describe("../escape")
    assert invalid.value.code == "invalid_prepared_id"
