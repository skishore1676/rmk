"""Private document staging and durable, at-most-once cloud posting.

The caller owns host-path allowlisting. This module additionally refuses
symlinks, copies bounded PDF/EPUB bytes into a private immutable preparation,
and records a receipt before doing any network work. A failed or interrupted
upload is never silently attempted again. Metadata readback confirms cloud
presence only; it cannot establish that the tablet has synced the content.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .cloud import (
    MAX_DOCUMENT_BYTES,
    CloudClient,
    CloudError,
    open_document_file,
    read_document_file,
    validate_document_name,
    validate_parent_path,
)


class PostingError(Exception):
    """A safe explanation for the plugin; no provider output or secrets."""

    def __init__(self, message: str, *, code: str = "posting_error"):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _private_directory(path: Path) -> None:
    """Create owned storage without accepting symlinked directory components."""
    fd = None
    try:
        if len(path.parts) < 2:
            raise PostingError("Posting storage must be a dedicated directory.", code="invalid_storage")
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=fd)
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        os.fchmod(fd, 0o700)
    except OSError:
        raise PostingError("Private posting storage is unavailable or uses symbolic links.", code="storage_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path: Path, data: dict) -> None:
    """Atomic 0600 JSON with file and directory durability before returning."""
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict:
    try:
        with open_document_file(path) as stream:
            if os.fstat(stream.fileno()).st_size > 64 * 1024:
                raise ValueError("oversized record")
            data = stream.read(64 * 1024 + 1)
            if len(data) > 64 * 1024:
                raise ValueError("oversized record")
            value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("invalid record")
        return value
    except (OSError, ValueError, CloudError):
        raise PostingError("A posting record is missing or unreadable. Check the posting history before another upload.", code="invalid_record") from None


def _prepared_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PostingError("The preparation ID is invalid.", code="invalid_prepared_id")
    return value


def _request_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise PostingError("Use a request ID of 1–128 letters, digits, periods, underscores, or hyphens, starting with a letter or digit.", code="invalid_request_id")
    return value


class PostingStore:
    """One user's persistent preparations and posting receipts.

    All operations are local except :meth:`post`. Its global advisory lock
    serializes posting across processes that share this state directory.
    ``status`` reads the atomic receipt without waiting for network activity.
    """

    def __init__(self, state_dir: str | Path, client: CloudClient, *, max_bytes: int = MAX_DOCUMENT_BYTES):
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_DOCUMENT_BYTES:
            raise PostingError("The document size limit is invalid.", code="invalid_limit")
        self.state_dir = Path(os.path.abspath(state_dir))
        self.client = client
        self.max_bytes = max_bytes
        self.prepared_dir = self.state_dir / "prepared"
        self.receipts_dir = self.state_dir / "receipts"
        for path in (self.state_dir, self.prepared_dir, self.receipts_dir):
            _private_directory(path)

    @contextmanager
    def _lock(self):
        fd = None
        try:
            fd = os.open(self.state_dir / "posting.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                fd = None
                raise PostingError("The posting lock is invalid.", code="invalid_storage")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            if fd is not None:
                os.close(fd)
            raise PostingError("Posting storage could not be locked.", code="storage_unavailable") from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _receipt_path(self, request_id: str) -> Path:
        # Validation is intentional even though the filename is a digest.
        request_id = _request_id(request_id)
        key = hashlib.sha256(request_id.encode("ascii")).hexdigest()
        return self.receipts_dir / f"{key}.json"

    def prepare_file(self, path: str | Path, name: str | None = None, parent: str = "") -> dict:
        """Snapshot a selected PDF/EPUB; no network activity and no upload.

        ``path`` must already be allowed by the plugin's host-root policy.
        The source may subsequently change or disappear without changing the
        preparation. Returned metadata intentionally omits all host paths.
        """
        try:
            name = validate_document_name(name if name is not None else Path(path).stem)
            parent = validate_parent_path(parent)
            data, media_type = read_document_file(path, max_bytes=self.max_bytes)
        except (CloudError, TypeError, ValueError) as exc:
            if isinstance(exc, CloudError):
                raise PostingError(str(exc), code=exc.code) from None
            raise PostingError("The selected document is invalid.", code="invalid_file") from None
        sha256 = hashlib.sha256(data).hexdigest()
        identity = json.dumps([sha256, name, parent, media_type], ensure_ascii=True, separators=(",", ":"))
        prepared_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        folder = self.prepared_dir / prepared_id
        suffix = ".pdf" if media_type == "application/pdf" else ".epub"
        metadata = {
            "schema_version": 1,
            "prepared_id": prepared_id,
            "name": name,
            "parent": parent,
            "cloud_path": f"{parent}/{name}",
            "sha256": sha256,
            "bytes": len(data),
            "media_type": media_type,
            "prepared_at": _now(),
        }
        with self._lock():
            if (folder / "metadata.json").exists():
                return self.describe(prepared_id)
            _private_directory(folder)
            staged = folder / f"{name}{suffix}"
            # An incomplete preparation after a crash can only be reused if
            # its bytes are exactly the expected immutable snapshot.
            if staged.exists() or staged.is_symlink():
                existing, _ = read_document_file(staged, max_bytes=self.max_bytes)
                if existing != data:
                    raise PostingError("An incomplete preparation contains unexpected bytes.", code="invalid_record")
            else:
                fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                _fsync_directory(folder)
            _write_json(folder / "metadata.json", metadata)
        return dict(metadata)

    def describe(self, prepared_id: str) -> dict:
        """Read preparation metadata and verify its content identity."""
        prepared_id = _prepared_id(prepared_id)
        metadata = _read_json(self.prepared_dir / prepared_id / "metadata.json")
        try:
            if metadata["prepared_id"] != prepared_id or metadata["schema_version"] != 1:
                raise ValueError("mismatched preparation")
            validate_document_name(metadata["name"])
            parent = validate_parent_path(metadata["parent"])
            if (
                parent != metadata["parent"]
                or metadata["media_type"] not in ("application/pdf", "application/epub+zip")
                or metadata["cloud_path"] != f"{parent}/{metadata['name']}"
                or not isinstance(metadata["bytes"], int)
                or isinstance(metadata["bytes"], bool)
                or not 1 <= metadata["bytes"] <= self.max_bytes
                or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
            ):
                raise ValueError("invalid preparation")
            identity = json.dumps([metadata["sha256"], metadata["name"], parent, metadata["media_type"]], ensure_ascii=True, separators=(",", ":"))
            if hashlib.sha256(identity.encode("utf-8")).hexdigest() != prepared_id:
                raise ValueError("changed preparation")
        except (KeyError, TypeError, ValueError, CloudError):
            raise PostingError("The preparation metadata is invalid.", code="invalid_record") from None
        return metadata

    def status(self, request_id: str) -> dict:
        """Return the saved receipt. This method never sends or retries."""
        path = self._receipt_path(request_id)
        if not path.exists() and not path.is_symlink():
            raise PostingError("No posting receipt exists for that request ID.", code="request_not_found")
        receipt = _read_json(path)
        if receipt.get("request_id") != request_id:
            raise PostingError("The posting receipt is invalid.", code="invalid_record")
        return receipt

    def _save_receipt(self, receipt: dict, **changes) -> dict:
        receipt = {**receipt, **changes, "updated_at": _now()}
        _write_json(self._receipt_path(receipt["request_id"]), receipt)
        return receipt

    def post(self, prepared_id: str, request_id: str) -> dict:
        """Attempt one new-document upload, with a durable receipt first.

        A repeated request returns its receipt exactly as saved. In particular,
        pending/unknown outcomes never upload again. A preparation is claimed
        by one request, so choosing a new request ID cannot bypass this rule.
        """
        prepared_id = _prepared_id(prepared_id)
        request_id = _request_id(request_id)
        receipt_path = self._receipt_path(request_id)
        with self._lock():
            if receipt_path.exists() or receipt_path.is_symlink():
                receipt = self.status(request_id)
                if receipt.get("prepared_id") != prepared_id:
                    raise PostingError("That request ID already belongs to a different preparation.", code="request_conflict")
                return receipt
            metadata = self.describe(prepared_id)
            folder = self.prepared_dir / prepared_id
            claim_path = folder / "claim.json"
            if claim_path.exists() or claim_path.is_symlink():
                if _read_json(claim_path).get("request_id") != request_id:
                    raise PostingError("This preparation already has a posting request. Inspect its existing receipt before preparing another upload.", code="prepared_already_used")
            suffix = ".pdf" if metadata["media_type"] == "application/pdf" else ".epub"
            staged = folder / f"{metadata['name']}{suffix}"
            try:
                data, media_type = read_document_file(staged, max_bytes=self.max_bytes)
            except CloudError:
                raise PostingError("The prepared document is unavailable or changed; no upload was attempted.", code="preparation_changed") from None
            if hashlib.sha256(data).hexdigest() != metadata["sha256"] or len(data) != metadata["bytes"] or media_type != metadata["media_type"]:
                raise PostingError("The prepared document has changed; no upload was attempted.", code="preparation_changed")
            _write_json(claim_path, {"request_id": request_id})
            # Persist before even the read-only cloud preflight. If this save
            # fails, no network activity has happened.
            receipt = self._save_receipt({
                **metadata,
                "request_id": request_id,
                "created_at": _now(),
                "status": "pending",
                "stage": "checking_destination",
                "upload_attempted": False,
                "tablet_sync": "unknown",
                "cloud_content_verified": False,
                "retry_policy": "never_automatically_reupload",
            })
            try:
                existing = self.client.stat_document(metadata["name"], metadata["parent"])
            except Exception:
                return self._save_receipt(receipt, status="blocked", stage="complete", reason="destination_check_unavailable")
            if existing is not None:
                return self._save_receipt(receipt, status="blocked", stage="complete", reason="destination_already_exists")
            receipt = self._save_receipt(receipt, stage="uploading", upload_attempted=True)
            try:
                self.client.upload_file(staged, name=metadata["name"], parent=metadata["parent"])
            except Exception:
                # The service may have accepted the document before the
                # client timed out. Never leak a provider exception or retry.
                return self._save_receipt(receipt, status="unknown", stage="complete", reason="upload_result_uncertain")
            # Save command success before readback: losing a later check does
            # not lose the fact that rmapi reported a successful submission.
            receipt = self._save_receipt(receipt, status="submitted", stage="checking_cloud", reason="upload_command_succeeded")
            try:
                observed = self.client.stat_document(metadata["name"], metadata["parent"])
            except Exception:
                return self._save_receipt(receipt, stage="complete", reason="cloud_readback_unavailable")
            if (
                isinstance(observed, dict)
                and observed.get("id")
                and observed.get("name") == metadata["name"]
                and observed.get("cloud_path") == metadata["cloud_path"]
                and observed.get("type") == "DocumentType"
                and observed.get("verification") == "exact_path_metadata"
            ):
                return self._save_receipt(receipt, status="cloud_confirmed", stage="complete", reason="exact_cloud_document_observed", cloud_document_id=observed["id"])
            return self._save_receipt(receipt, stage="complete", reason="exact_cloud_document_not_observed")
