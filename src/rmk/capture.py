"""Private immutable captures with one atomic capture checkpoint.

This checkpoint says nothing about interpretation, delivery, or tablet freshness.
Incomplete staging directories are retained for diagnosis, never treated as success.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile

import platformdirs

from .render import CAPTURE_RENDER_VERSION, render_tiles
from .snapshot import checked_id, digest, read_snapshot
from .transport.local import LocalTransport

SCHEMA = "rmk.capture.v1"


class CaptureError(RuntimeError):
    pass


def default_state_dir() -> Path:
    return Path(platformdirs.user_data_dir("rmk")) / "capture"


def _json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as fh:
        path.chmod(0o600)
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, value: dict) -> None:
    # A failed replace leaves the prior checkpoint intact and retains the temp.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".checkpoint-", delete=False) as fh:
        fh.write(_json_bytes(value))
        fh.flush()
        os.fsync(fh.fileno())
        temp = Path(fh.name)
    os.replace(temp, path)
    _fsync_dir(path.parent)


def _private_dir(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise CaptureError("Capture directory must belong to the current user")
    path.chmod(0o700)


def _validate_destination(destination: Path, source: Path | None) -> Path:
    destination = destination.expanduser().resolve()
    if source is not None:
        source = source.expanduser().resolve()
        if destination == source or source in destination.parents or destination in source.parents:
            raise CaptureError("Capture state must be separate from the source store")
    if any((p / ".git").exists() for p in (destination, *destination.parents)):
        raise CaptureError("Keep private captures outside Git")
    return destination


def changes(previous: dict | None, current: dict) -> dict:
    before = {p["id"]: p["sha256"] for p in previous["pages"]} if previous else {}
    after = {p["id"]: p["sha256"] for p in current["pages"]}
    common_before = [pid for pid in before if pid in after]
    common_after = [pid for pid in after if pid in before]
    return {
        "baseline": previous is None,
        "added": [] if previous is None else [pid for pid in after if pid not in before],
        "edited": [pid for pid in after if pid in before and after[pid] != before[pid]],
        "removed": [pid for pid in before if pid not in after],
        "reordered": previous is not None and common_before != common_after,
        "renamed": previous is not None and previous["name"] != current["name"],
    }


class CaptureStore:
    def __init__(self, state_dir: Path, transport, root: str, notebook_id: str):
        checked_id(notebook_id)
        if isinstance(transport, LocalTransport):
            root = str(Path(root).expanduser().resolve())
            source = Path(root)
            kind = "local"
        else:
            raise CaptureError("Capture state currently requires a local desktop transport")
        self.base = _validate_destination(state_dir, source)
        self.transport, self.root, self.notebook_id = transport, root, notebook_id
        self.source_id = digest(_json_bytes({"kind": kind, "root": root}))
        self.directory = self.base / self.source_id / notebook_id

    @contextmanager
    def _lock(self):
        for path in (self.base, self.directory.parent, self.directory):
            _private_dir(path)
        fd = os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CaptureError("Another capture is running for this notebook; retry later") from None
            yield
        finally:
            os.close(fd)

    def _load(self) -> tuple[dict | None, dict | None]:
        state_path = self.directory / "state.json"
        if not state_path.exists():
            return None, None
        try:
            state = json.loads(state_path.read_bytes())
            revision = state["revision"]
            if (state["schema"] != SCHEMA or state["notebook_id"] != self.notebook_id
                    or state["source_id"] != self.source_id or len(revision) != 64
                    or any(c not in "0123456789abcdef" for c in revision)):
                raise ValueError("Invalid state identity")
            manifest_path = self.directory / "revisions" / revision / "manifest.json"
            data = manifest_path.read_bytes()
            if digest(data) != state["manifest_sha256"]:
                raise ValueError("Manifest checksum mismatch")
            manifest = self._validate_revision(manifest_path.parent)
            if manifest["revision"] != revision:
                raise ValueError("Revision mismatch")
            if not isinstance(state["changes"], dict) or not isinstance(state["captured_at"], str):
                raise ValueError("Invalid checkpoint")
            return state, manifest
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise CaptureError("Corrupt capture state/artifacts; preserve evidence and recover explicitly") from exc

    def _validate_revision(self, directory: Path) -> dict:
        manifest = json.loads((directory / "manifest.json").read_bytes())
        if (manifest["schema"] != SCHEMA or manifest["notebook_id"] != self.notebook_id
                or manifest["source_id"] != self.source_id):
            raise CaptureError("Invalid revision identity")
        expected_revision = digest(_json_bytes({"source_hashes": manifest["source_hashes"],
                                              "render_version": manifest["render_version"]}))
        if expected_revision != manifest["revision"]:
            raise CaptureError("Invalid revision checksum")
        pages = manifest["pages"]
        if manifest["active_order"] != [checked_id(p["id"]) for p in pages]:
            raise CaptureError("Invalid revision page order")
        source_paths = [f"{self.notebook_id}.metadata", f"{self.notebook_id}.content"]
        source_paths += [f"{self.notebook_id}/{p['id']}.rm" for p in pages]
        if set(manifest["source_hashes"]) != set(source_paths) or len(set(manifest["active_order"])) != len(pages):
            raise CaptureError("Incomplete revision source coverage")
        required_artifacts = {f"source/{path}" for path in source_paths}
        for path, checksum in manifest["source_hashes"].items():
            if manifest["artifact_hashes"].get(f"source/{path}") != checksum:
                raise CaptureError("Source artifact checksum mismatch")
        for page in pages:
            if page["sha256"] != manifest["source_hashes"][f"{self.notebook_id}/{page['id']}.rm"]:
                raise CaptureError("Page checksum mismatch")
            if page["height"] <= 0 or page["width"] <= 0 or not page["tiles"]:
                raise CaptureError("Incomplete page render")
            end = 0
            for index, tile in enumerate(page["tiles"], 1):
                expected_file = f"pages/{page['id']}/tile-{index:02d}.png"
                if (tile["file"] != expected_file or tile["y_start"] != (0 if index == 1 else end - 150)
                        or not tile["y_start"] < tile["y_end"] <= page["height"]):
                    raise CaptureError("Incomplete tile coverage")
                end = tile["y_end"]
                required_artifacts.add(expected_file)
            if end != page["height"]:
                raise CaptureError("Missing page bottom")
        if set(manifest["artifact_hashes"]) != required_artifacts:
            raise CaptureError("Incomplete revision artifacts")
        for filename, checksum in manifest["artifact_hashes"].items():
            path = directory / filename
            if Path(filename).is_absolute() or ".." in Path(filename).parts or path.is_symlink():
                raise CaptureError("Invalid artifact path")
            if digest(path.read_bytes()) != checksum:
                raise CaptureError("Corrupt revision artifact")
        return manifest

    def status(self) -> dict:
        state, manifest = self._load()
        return {"schema": SCHEMA, "notebook_id": self.notebook_id,
                "capture": state, "artifact": str(self.directory / "revisions" / state["revision"]) if state else None,
                "page_count": len(manifest["pages"]) if manifest else 0,
                "sync_freshness": "unknown", "interpretation": "not_implemented", "delivery": "not_implemented"}

    def capture(self) -> dict:
        with self._lock():
            state, prior = self._load()
            snapshot = read_snapshot(self.transport, self.root, self.notebook_id)
            revision = digest(_json_bytes({"source_hashes": snapshot.source_hashes,
                                          "render_version": CAPTURE_RENDER_VERSION}))
            observed_at = datetime.now(timezone.utc).isoformat()
            if state and state["revision"] == revision:
                snapshot.verify(self.transport, self.root)
                return {"revision": revision, "observed_at": observed_at, "unchanged": True,
                        "changes": changes(prior, prior), "artifact": self.status()["artifact"]}
            revisions = self.directory / "revisions"
            _private_dir(revisions)
            target = revisions / revision
            if target.exists():
                # Recover an interrupted checkpoint publication, never overwrite artifacts.
                manifest = self._validate_revision(target)
            else:
                stage = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=revisions))
                for path, data in snapshot.observed.items():
                    dest = stage / "source" / path
                    _private_dir(dest.parent)
                    _write(dest, data)
                pages = []
                for pid, data in snapshot.pages:
                    output = stage / "pages" / pid
                    _private_dir(output)
                    rendered = render_tiles(data, output)
                    for tile in rendered["tiles"]:
                        tile["file"] = f"pages/{pid}/{tile['file']}"
                    pages.append({"id": pid, "sha256": digest(data), **rendered})
                artifact_hashes = {str(p.relative_to(stage)): digest(p.read_bytes())
                                   for p in stage.rglob("*") if p.is_file()}
                manifest = {"schema": SCHEMA, "notebook_id": self.notebook_id, "source_id": self.source_id,
                            "revision": revision, "name": snapshot.metadata.get("visibleName"),
                            "source_last_modified_ms": snapshot.metadata.get("lastModified"),
                            "observed_at": observed_at, "source_hashes": snapshot.source_hashes,
                            "active_order": [pid for pid, _ in snapshot.pages], "pages": pages,
                            "coverage": "All declared active pages; orphan/deleted files excluded",
                            "sync_freshness": "unknown", "render_version": CAPTURE_RENDER_VERSION,
                            "artifact_hashes": artifact_hashes}
                _write(stage / "manifest.json", _json_bytes(manifest))
                self._validate_revision(stage)
                snapshot.verify(self.transport, self.root)
                # Flush rendered artifacts and directory entries before publishing pointer.
                for p in stage.rglob("*"):
                    if p.is_file():
                        with p.open("rb") as fh:
                            os.fsync(fh.fileno())
                    elif p.is_dir():
                        _fsync_dir(p)
                _fsync_dir(stage)
                os.rename(stage, target)
                _fsync_dir(revisions)
            snapshot.verify(self.transport, self.root)
            delta = changes(prior, manifest)
            checkpoint = {"schema": SCHEMA, "notebook_id": self.notebook_id, "source_id": self.source_id,
                          "revision": revision, "previous_revision": state["revision"] if state else None,
                          "manifest_sha256": digest((target / "manifest.json").read_bytes()),
                          "captured_at": observed_at, "changes": delta}
            _atomic_write(self.directory / "state.json", checkpoint)
            return {"revision": revision, "observed_at": observed_at, "unchanged": False,
                    "changes": delta, "artifact": str(target)}
