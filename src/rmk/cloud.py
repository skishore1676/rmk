"""Native reMarkable Cloud connection: register this machine, push via rmapi.

This is the write side rmk was missing. The read side (``transport``) pulls
notes *off* the tablet; this module connects the machine to the cloud and
pushes a document *onto* it, so it shows up in the tablet's library without
USB or SSH.

Auth (two steps, from the public rmapy reference implementation):
1. Register a device with a one-time code from
   https://my.remarkable.com/device/desktop/connect -> device token.
2. Exchange the device token for a user token, used for API calls.

Upload goes through the maintained ``rmapi`` tool (ddvk/rmapi fork), which
speaks the current sync protocol (v3/v4). The older v2 document-storage API
is retired (it 404s), and the original rmapi's v2 sync path returns 410 —
so rmk does not implement upload itself; it delegates to ``rmapi put``.
rmapi keeps its own token store (``~/.rmapi`` or ``~/.config/rmapi/``),
so it needs its own one-time registration the first time it runs.

Tokens live in ``~/.config/rmk/cloud-tokens.json`` (0600), never in the repo
and never in ``config.toml``. Only the standard library is used for HTTP, so
no new dependency. Tests stub the HTTP layer and the rmapi runner; nothing
here touches the network unless a command runs.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

import platformdirs

AUTH_BASE_URL = "https://webapp-prod.cloud.remarkable.engineering"
# NOTE (2026-10-14): the auth host has moved twice. The 2021-era appspot host
# 404s on /token/json/2/device/new, and my.remarkable.com serves the website
# there (GET 200, POST 405). The live auth host is
# webapp-prod.cloud.remarkable.engineering (verified against the maintained
# rmapi client's config/url.go).
DEVICE_TOKEN_URL = AUTH_BASE_URL + "/token/json/2/device/new"
USER_TOKEN_URL = AUTH_BASE_URL + "/token/json/2/user/new"
DEVICE_DESC = "desktop-linux"  # matches the maintained rmapi client
USER_AGENT = "rmk/0.1.0"


class CloudError(Exception):
    """Anything the reMarkable Cloud side went wrong (auth, upload, API)."""

    def __init__(self, message: str, *, code: str = "cloud_error"):
        super().__init__(message)
        self.code = code


MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


def validate_document_name(name: str) -> str:
    """Accept one unambiguous document/folder name, never rmapi syntax."""
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029" for c in name)
        or len(name.encode("utf-8")) > 180
        or name.startswith(("-", ".", "~"))
        or any(c in name for c in "/\\*?[]{}")
    ):
        raise CloudError("Use a plain document name without paths, control characters, or command syntax.", code="invalid_name")
    return name


def validate_parent_path(parent: str) -> str:
    """Normalize the root; all other rmapi destinations must be absolute."""
    if parent in ("", "/"):
        return ""
    if not isinstance(parent, str) or not parent.startswith("/") or any(unicodedata.category(c).startswith("C") for c in parent) or len(parent.encode("utf-8")) > 1024:
        raise CloudError("The destination must be an absolute reMarkable folder path.", code="invalid_parent")
    try:
        for part in parent[1:].split("/"):
            validate_document_name(part)
    except CloudError:
        raise CloudError("The destination contains an invalid folder name or traversal component.", code="invalid_parent") from None
    return parent


@contextmanager
def open_document_file(path: str | Path):
    """Open one regular file without following symlinks in any component.

    Walking via directory descriptors also closes the usual check/open race.
    The plugin separately restricts which host roots may be selected.
    """
    raw = os.fspath(path)
    if not isinstance(raw, str) or ".." in Path(raw).parts or any(unicodedata.category(c).startswith("C") for c in raw):
        raise CloudError("The selected file path is invalid.", code="invalid_file")
    absolute = Path(os.path.abspath(raw))
    directory_fd = None
    file_fd = None
    try:
        directory_fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for part in absolute.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise CloudError("The selected document must be a regular file.", code="invalid_file")
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            yield stream
    except OSError:
        raise CloudError("The selected file is unavailable or uses a symbolic link.", code="invalid_file") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def read_document_file(path: str | Path, *, max_bytes: int = MAX_DOCUMENT_BYTES) -> tuple[bytes, str]:
    """Bound the read and check PDF/EPUB magic before any external action."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in (".pdf", ".epub"):
        raise CloudError("Only .pdf and .epub documents can be pushed.", code="invalid_file_type")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1 or max_bytes > MAX_DOCUMENT_BYTES:
        raise CloudError("The document size limit is invalid.", code="invalid_limit")
    with open_document_file(p) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size < 1 or before.st_size > max_bytes:
            raise CloudError("The selected document is empty or exceeds the configured size limit.", code="invalid_file_size")
        data = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
    if len(data) > max_bytes or len(data) != before.st_size or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
        raise CloudError("The selected document changed while it was being read, or exceeds the size limit.", code="file_changed")
    if suffix == ".pdf":
        if not re.match(rb"%PDF-\d\.\d", data) or b"%%EOF" not in data[-2048:]:
            raise CloudError("The selected file does not contain recognizable PDF data.", code="invalid_file_magic")
        return data, "application/pdf"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(e.file_size for e in entries) > 4 * MAX_DOCUMENT_BYTES:
                raise ValueError("archive too large")
            for entry in entries:
                parts = entry.filename.replace("\\", "/").split("/")
                if entry.filename.startswith(("/", "\\")) or ".." in parts or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                    raise ValueError("unsafe archive member")
            mime = archive.getinfo("mimetype")
            if mime.file_size != len(b"application/epub+zip") or archive.read(mime) != b"application/epub+zip" or "META-INF/container.xml" not in archive.namelist():
                raise ValueError("not an EPUB")
    except (zipfile.BadZipFile, KeyError, ValueError, RuntimeError, NotImplementedError, OSError):
        raise CloudError("The selected file does not contain a supported EPUB archive.", code="invalid_file_magic") from None
    return data, "application/epub+zip"


def token_path() -> Path:
    """Where the device/user tokens live (0600, outside the repo)."""
    return Path(platformdirs.user_config_dir("rmk")) / "cloud-tokens.json"


def find_rmapi() -> Path | None:
    """Locate the rmapi binary: PATH first, then the usual install spots."""
    on_path = shutil.which("rmapi")
    if on_path:
        return Path(on_path)
    for candidate in (
        Path.home() / "go" / "bin" / "rmapi",
        Path("/opt/homebrew/bin/rmapi"),
        Path("/usr/local/bin/rmapi"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _rmapi_registered() -> bool:
    """Whether rmapi has saved its own tokens (it registers separately).

    rmapi checks ``~/.rmapi`` first, else the OS config dir
    (``~/Library/Application Support/rmapi/rmapi.conf`` on macOS,
    ``~/.config/rmapi/rmapi.conf`` on Linux).
    """
    if (Path.home() / ".rmapi").is_file():
        return True
    return (Path(platformdirs.user_config_dir("rmapi")) / "rmapi.conf").is_file()


def _default_runner(argv: list[str]):
    """Run rmapi and capture output (injectable for tests)."""
    # Never let a plugin request wait for a one-time registration code.
    # Trace logs can contain credentials; they are unnecessary for this call.
    env = dict(os.environ)
    env.pop("RMAPI_TRACE", None)
    return subprocess.run(argv, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL, env=env)


def _default_request(method: str, url: str, body: bytes | None, headers: dict) -> tuple[int, bytes]:
    """One HTTP round trip via the standard library.

    Returns ``(status, body_bytes)``. Raises :class:`CloudError` on failure
    (including HTTP error statuses, whose bodies are surfaced for diagnosis).
    """
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        raise CloudError(f"{method} {url} -> HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise CloudError(f"{method} {url} -> network error: {e}") from e


class CloudClient:
    """A reMarkable Cloud session. ``request``/``runner`` injectable for tests."""

    def __init__(self, path: Path | None = None, request=None, runner=None, *, require_legacy_auth: bool = True):
        self._path = path or token_path()
        self._request = request or _default_request
        self._runner = runner or _default_runner
        # The original CLI required rmk's separately registered device token.
        # rmapi owns the real upload credentials. Plugins can use their
        # already-paired rmapi session without reading or renewing rmk tokens.
        self.require_legacy_auth = require_legacy_auth
        self.device_token: str = ""
        self.user_token: str = ""
        if require_legacy_auth:
            self._load()

    # -- tokens -------------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return
        self.device_token = data.get("devicetoken", "")
        self.user_token = data.get("usertoken", "")

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps({"devicetoken": self.device_token, "usertoken": self.user_token}).encode())
        finally:
            os.close(fd)
        # Belt and braces: make sure the file is private even if it existed.
        os.chmod(self._path, 0o600)

    @property
    def is_registered(self) -> bool:
        return bool(self.device_token)

    @property
    def is_authed(self) -> bool:
        return bool(self.device_token and self.user_token)

    # -- auth ---------------------------------------------------------------
    def register(self, code: str) -> None:
        """Register this machine as a device with a one-time code.

        The code comes from https://my.remarkable.com/device/desktop/connect
        and can only be used once. Fetches the device token, then the user
        token, and saves both.
        """
        body = json.dumps(
            {"code": code, "deviceDesc": DEVICE_DESC, "deviceID": str(uuid.uuid4())}
        ).encode()
        status, resp = self._request(
            "POST", DEVICE_TOKEN_URL, body, {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        )
        if status != 200 or not resp:
            raise CloudError("Device registration failed — the code may be expired or already used. Get a fresh code and try again.")
        self.device_token = resp.decode("utf-8", "replace").strip()
        # Save immediately: the one-time code is spent once device/new
        # succeeds, so a later renew() failure must not lose the device token
        # (and must not cost the user a fresh code).
        self._save()
        self.renew()
        self._save()

    def renew(self) -> None:
        """Fetch a fresh user token from the stored device token."""
        if not self.device_token:
            raise CloudError("No device registered. Run `rmk cloud-register` first.")
        status, resp = self._request(
            "POST", USER_TOKEN_URL, None,
            {"Authorization": f"Bearer {self.device_token}", "User-Agent": USER_AGENT},
        )
        if status != 200 or not resp:
            raise CloudError("Could not renew the user token. Re-register with a fresh code.")
        self.user_token = resp.decode("utf-8", "replace").strip()
        self._save()

    # -- documents (via rmapi) ---------------------------------------------
    def _rmapi(self) -> Path:
        """The rmapi binary, or a clear error telling the user to install it."""
        rmapi = find_rmapi()
        if rmapi is None:
            raise CloudError(
                "rmapi not found. Install the maintained ddvk/rmapi fork "
                "(e.g. `go install` from github.com/ddvk/rmapi) and make sure "
                "it is on PATH or in ~/go/bin."
            )
        return rmapi

    def _run_rmapi(self, *args: str):
        """Run rmapi without exposing potentially credential-bearing output."""
        try:
            result = self._runner([str(self._rmapi()), *args])
        except (OSError, subprocess.TimeoutExpired):
            raise CloudError("The rmapi command failed or timed out; its outcome may be unknown.", code="rmapi_unavailable") from None
        if result.returncode != 0:
            raise CloudError("The rmapi command failed. Check the connection locally before retrying.", code="rmapi_failed")
        return result

    def stat_document(self, name: str, parent: str = "") -> dict | None:
        """Read metadata at one exact cloud path; never infer tablet sync.

        ``rmapi stat`` uses NodeByPath (not ls glob matching) and emits the
        Document JSON. An absent entry is distinct from an unavailable cloud.
        No raw stdout/stderr is returned to plugin callers.
        """
        name = validate_document_name(name)
        parent = validate_parent_path(parent)
        target = f"{parent}/{name}"
        if not _rmapi_registered():
            raise CloudError("rmapi is not registered yet.", code="not_registered")
        try:
            result = self._runner([str(self._rmapi()), "-ni", "stat", target])
        except (OSError, subprocess.TimeoutExpired):
            raise CloudError("Cloud metadata could not be checked.", code="stat_unavailable") from None
        if result.returncode != 0:
            # This is rmapi stat's explicit missing-entry diagnostic. Other
            # failures must not be mistaken for a free destination name.
            diagnostic = (result.stderr or "") + "\n" + (result.stdout or "")
            if "file doesn't exist" in diagnostic.lower():
                return None
            raise CloudError("Cloud metadata could not be checked.", code="stat_unavailable")
        try:
            output = result.stdout or ""
            if len(output) > 1024 * 1024:
                raise ValueError("oversized metadata")
            metadata = json.loads(output)
            document_id = str(uuid.UUID(metadata["ID"]))
            parent_id = metadata.get("Parent", "")
            if parent_id:
                parent_id = str(uuid.UUID(parent_id))
            if metadata["Name"] != name or metadata["Type"] not in ("DocumentType", "CollectionType"):
                raise ValueError("mismatched metadata")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CloudError("Cloud metadata did not identify the exact requested entry.", code="invalid_metadata") from None
        return {
            "id": document_id,
            "name": name,
            "parent_id": parent_id,
            "type": metadata["Type"],
            "cloud_path": target,
            "verification": "exact_path_metadata",
        }

    def list_documents(self) -> list[dict]:
        """Top-level documents/folders in the cloud (names only)."""
        if not _rmapi_registered():
            raise CloudError("rmapi is not registered yet — run `rmapi ls` and enter a fresh one-time code when prompted.")
        result = self._run_rmapi("ls")
        docs = []
        for line in (result.stdout or "").splitlines():
            if line.startswith(("[f]", "[d]")) and "\t" in line:
                kind, name = line.split("\t", 1)
                docs.append({"kind": kind, "name": name.strip()})
        return docs

    def upload_file(self, path: str | Path, name: str | None = None, parent: str = "") -> dict:
        """Upload a PDF or EPUB file as a new document via ``rmapi put``.

        rmapi names the document after the file, so a ``--name`` that differs
        from the file's stem uploads a temp copy under that name. ``parent``
        is an rmapi folder path (default: library root). Returns the
        document's name and local path.
        """
        p = Path(os.path.abspath(path))
        display = validate_document_name(name if name is not None else p.stem)
        parent = validate_parent_path(parent)
        data, _ = read_document_file(path)
        if self.require_legacy_auth and not self.is_authed:
            raise CloudError("Not connected to the reMarkable Cloud. Run `rmk cloud-register --code CODE` first.")
        if not _rmapi_registered():
            raise CloudError("rmapi is not registered yet — run `rmapi ls` and enter a fresh one-time code when prompted.")
        if display == p.stem and p.suffix == p.suffix.lower():
            self._upload_via_rmapi(p, parent)
        else:
            with tempfile.TemporaryDirectory(prefix="rmk-push-") as tmp:
                staged = Path(tmp).resolve() / f"{display}{p.suffix.lower()}"
                fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                self._upload_via_rmapi(staged, parent)
        return {"name": display, "path": str(p)}

    def _upload_via_rmapi(self, file: Path, parent: str) -> None:
        argv = ["put", str(file)]
        if parent:
            argv.append(parent)
        self._run_rmapi(*argv)


def token_file_mode(path: Path) -> int:
    """Permission bits of the token file (for tests/diagnostics)."""
    return stat.S_IMODE(path.stat().st_mode)
