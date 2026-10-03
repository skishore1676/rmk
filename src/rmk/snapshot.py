"""Strict selected-notebook reads; source transports are read-only."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid

from .transport.base import Transport


class SnapshotError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_id(value: str) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise SnapshotError("Expected a canonical notebook/page UUID") from None
    return value


def active_pages(content: dict) -> list[str]:
    """Declared active order only. Malformed modern order cannot fall back."""
    if not isinstance(content, dict):
        raise SnapshotError("Invalid notebook content")
    if "cPages" in content:
        modern = content["cPages"]
        if not isinstance(modern, dict) or not isinstance(modern.get("pages"), list):
            raise SnapshotError("Invalid modern page order")
        ids = []
        for item in modern["pages"]:
            if not isinstance(item, dict):
                raise SnapshotError("Invalid page entry")
            deleted = item.get("deleted", False)
            if isinstance(deleted, dict):
                deleted = deleted.get("value")
            if type(deleted) not in (bool, int) or deleted not in (False, True):
                raise SnapshotError("Invalid page deletion marker")
            if not deleted:
                ids.append(checked_id(item.get("id")))
    else:
        if not isinstance(content.get("pages"), list):
            raise SnapshotError("Missing declared page order")
        ids = [checked_id(pid) for pid in content["pages"]]
    if len(set(ids)) != len(ids):
        raise SnapshotError("Duplicate active page UUID")
    return ids


@dataclass
class Snapshot:
    notebook_id: str
    metadata: dict
    pages: list[tuple[str, bytes]]
    observed: dict[str, bytes]

    @property
    def source_hashes(self) -> dict[str, str]:
        return {path: digest(data) for path, data in self.observed.items()}

    def verify(self, transport: Transport, root: str) -> None:
        for path, data in self.observed.items():
            try:
                current = transport.read_bytes(f"{root.rstrip('/')}/{path}")
            except OSError as exc:
                raise SnapshotError("Source disappeared during capture") from exc
            if current != data:
                raise SnapshotError("Source changed during capture; retry")


def read_snapshot(transport: Transport, root: str, notebook_id: str) -> Snapshot:
    checked_id(notebook_id)
    root = root.rstrip("/")
    observed = {}

    def read(path):
        data = transport.read_bytes(f"{root}/{path}")
        observed[path] = data
        return data

    try:
        metadata = json.loads(read(f"{notebook_id}.metadata"))
        content = json.loads(read(f"{notebook_id}.content"))
    except (ValueError, UnicodeError) as exc:
        raise SnapshotError("Invalid source JSON") from exc
    if not isinstance(metadata, dict):
        raise SnapshotError("Invalid notebook metadata")
    if metadata.get("deleted") or metadata.get("parent") == "trash":
        raise SnapshotError("Selected notebook is deleted")
    if metadata.get("type") == "CollectionType":
        raise SnapshotError("Selected UUID is a folder")
    if not isinstance(content, dict) or content.get("fileType") != "notebook":
        raise SnapshotError("Only handwritten notebooks are supported")
    pages = [(pid, read(f"{notebook_id}/{pid}.rm")) for pid in active_pages(content)]
    if any(not data for _, data in pages):
        raise SnapshotError("Empty active stroke file")
    snapshot = Snapshot(notebook_id, metadata, pages, observed)
    snapshot.verify(transport, root)
    return snapshot
