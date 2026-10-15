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

import json
import os
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
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
    return subprocess.run(argv, capture_output=True, text=True, timeout=180)


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

    def __init__(self, path: Path | None = None, request=None, runner=None):
        self._path = path or token_path()
        self._request = request or _default_request
        self._runner = runner or _default_runner
        self.device_token: str = ""
        self.user_token: str = ""
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
        """Run rmapi, raising CloudError (with its output) on failure."""
        result = self._runner([str(self._rmapi()), *args])
        if result.returncode != 0:
            raise CloudError(f"rmapi {' '.join(args)} failed: {(result.stderr or result.stdout or '').strip()}")
        return result

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
        p = Path(path)
        if not p.is_file():
            raise CloudError(f"No such file: {p}")
        if p.suffix.lower() not in (".pdf", ".epub"):
            raise CloudError(f"Only .pdf and .epub can be pushed (got {p.suffix}).")
        if not self.is_authed:
            raise CloudError("Not connected to the reMarkable Cloud. Run `rmk cloud-register --code CODE` first.")
        if not _rmapi_registered():
            raise CloudError("rmapi is not registered yet — run `rmapi ls` and enter a fresh one-time code when prompted.")
        display = name or p.stem
        if display == p.stem:
            self._upload_via_rmapi(p, parent)
        else:
            with tempfile.TemporaryDirectory(prefix="rmk-push-") as tmp:
                staged = Path(tmp) / f"{display}{p.suffix}"
                staged.write_bytes(p.read_bytes())
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
