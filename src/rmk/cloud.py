"""Native reMarkable Cloud connection: push a PDF/EPUB onto the tablet.

This is the write side rmk was missing. The read side (``transport``) pulls
notes *off* the tablet; this module pushes a document *onto* it through the
same cloud sync the official apps use, so it shows up in the tablet's library
without USB or SSH.

Auth (two steps, from the public rmapy reference implementation):
1. Register a device with a one-time code from
   https://my.remarkable.com/device/desktop/connect -> device token.
2. Exchange the device token for a user token, used for all API calls.

Upload (three steps):
1. Build the reMarkable zip: ``<uuid>.content`` (JSON), ``<uuid>.pagedata``,
   ``<uuid>.pdf`` (or ``.epub``).
2. Ask the cloud for a blob upload URL (``upload/request``).
3. PUT the zip to that URL, then publish the metadata (``upload/update-status``).

Tokens live in ``~/.config/rmk/cloud-tokens.json`` (0600), never in the repo
and never in ``config.toml``. Only the standard library is used for HTTP, so
no new dependency. Tests stub the HTTP layer; nothing here touches the network
unless a command runs.

Field names follow the cloud API exactly — including ``VissibleName`` (the
API's own spelling, double ``s``).
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import platformdirs

AUTH_BASE_URL = "https://webapp-prod.cloud.remarkable.engineering"
# NOTE (2026-10-14): the auth host has moved twice. The 2021-era appspot host
# 404s on /token/json/2/device/new, and my.remarkable.com serves the website
# there (GET 200, POST 405). The live auth host is
# webapp-prod.cloud.remarkable.engineering (verified against the maintained
# rmapi client's config/url.go).
API_BASE_URL = "https://document-storage-production-dot-remarkable-production.appspot.com"
DEVICE_TOKEN_URL = AUTH_BASE_URL + "/token/json/2/device/new"
USER_TOKEN_URL = AUTH_BASE_URL + "/token/json/2/user/new"
DOCS_URL = API_BASE_URL + "/document-storage/json/2/docs"
UPLOAD_REQUEST_URL = API_BASE_URL + "/document-storage/json/2/upload/request"
UPDATE_STATUS_URL = API_BASE_URL + "/document-storage/json/2/upload/update-status"
DEVICE_DESC = "desktop-windows"
USER_AGENT = "rmk/0.1.0"

RFC3339 = "%Y-%m-%dT%H:%M:%SZ"


class CloudError(Exception):
    """Anything the reMarkable Cloud side went wrong (auth, upload, API)."""


def token_path() -> Path:
    """Where the device/user tokens live (0600, outside the repo)."""
    return Path(platformdirs.user_config_dir("rmk")) / "cloud-tokens.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(RFC3339)


def build_pdf_zip(doc_id: str, pdf_bytes: bytes, page_count: int = 1) -> bytes:
    """Build the reMarkable zip for a PDF document.

    Layout (matches what the cloud expects for an uploaded PDF):
    ``<uuid>.content`` — JSON describing the document,
    ``<uuid>.pagedata`` — empty page data,
    ``<uuid>.pdf`` — the PDF itself.
    """
    content = {
        "extraMetadata": {},
        "fileType": "pdf",
        "pageCount": page_count,
        "lastOpenedPage": 0,
        "lineHeight": -1,
        "margins": 180,
        "textScale": 1,
        "transform": {},
    }
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as zf:
        zf.writestr(f"{doc_id}.content", json.dumps(content))
        zf.writestr(f"{doc_id}.pagedata", "")
        zf.writestr(f"{doc_id}.pdf", pdf_bytes)
    buf.seek(0)
    return buf.read()


def build_epub_zip(doc_id: str, epub_bytes: bytes) -> bytes:
    """Build the reMarkable zip for an EPUB document."""
    content = {
        "extraMetadata": {},
        "fileType": "epub",
        "pageCount": 0,
        "lastOpenedPage": 0,
        "lineHeight": -1,
        "margins": 180,
        "textScale": 1,
        "transform": {},
    }
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as zf:
        zf.writestr(f"{doc_id}.content", json.dumps(content))
        zf.writestr(f"{doc_id}.pagedata", "")
        zf.writestr(f"{doc_id}.epub", epub_bytes)
    buf.seek(0)
    return buf.read()


def _metadata_payload(doc_id: str, name: str, parent: str) -> dict:
    """The update-status body for a brand-new document (Version 1)."""
    return {
        "ID": doc_id,
        "Version": 1,
        "Message": "",
        "Success": True,
        "BlobURLGet": "",
        "BlobURLGetExpires": "",
        "BlobURLPut": "",
        "BlobURLPutExpires": "",
        "ModifiedClient": _now(),
        "Type": "DocumentType",
        "VissibleName": name,
        "CurrentPage": 1,
        "Bookmarked": False,
        "Parent": parent,
    }


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
    """A reMarkable Cloud session. ``request`` is injectable for tests."""

    def __init__(self, path: Path | None = None, request=None):
        self._path = path or token_path()
        self._request = request or _default_request
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

    def _authed(self, method: str, url: str, body: bytes | None = None) -> tuple[int, bytes]:
        if not self.user_token:
            raise CloudError("No user token. Run `rmk cloud-register` first.")
        headers = {"Authorization": f"Bearer {self.user_token}", "User-Agent": USER_AGENT}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self._request(method, url, body, headers)

    # -- documents ----------------------------------------------------------
    def list_documents(self) -> list[dict]:
        """All documents/folders in the cloud (metadata only)."""
        status, resp = self._authed("GET", DOCS_URL)
        if status != 200:
            raise CloudError(f"Listing documents failed (HTTP {status}).")
        return json.loads(resp.decode("utf-8", "replace"))

    def upload_file(self, path: str | Path, name: str | None = None, parent: str = "") -> dict:
        """Upload a PDF or EPUB file as a new document. Returns its metadata."""
        p = Path(path)
        if not p.is_file():
            raise CloudError(f"No such file: {p}")
        raw = p.read_bytes()
        suffix = p.suffix.lower()
        doc_id = str(uuid.uuid4())
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader

                page_count = len(PdfReader(BytesIO(raw)).pages)
            except Exception:
                page_count = 1
            blob = build_pdf_zip(doc_id, raw, page_count)
        elif suffix == ".epub":
            blob = build_epub_zip(doc_id, raw)
        else:
            raise CloudError(f"Only .pdf and .epub can be pushed (got {p.suffix}).")
        display = name or p.stem
        return self._upload_blob(doc_id, blob, display, parent)

    def _upload_blob(self, doc_id: str, blob: bytes, name: str, parent: str) -> dict:
        # 1. Ask for a blob upload URL.
        req_body = json.dumps([{"ID": doc_id, "Type": "DocumentType", "Version": 1}]).encode()
        status, resp = self._authed("PUT", UPLOAD_REQUEST_URL, req_body)
        if status != 200:
            raise CloudError(f"Upload request failed (HTTP {status}).")
        try:
            put_url = json.loads(resp.decode("utf-8", "replace"))[0]["BlobURLPut"]
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise CloudError("Upload request returned no blob URL.") from e
        # 2. PUT the zip to the blob URL (no auth header — the URL is signed).
        status, _ = self._request("PUT", put_url, blob, {"Content-Type": "application/octet-stream"})
        if status not in (200, 201, 204):
            raise CloudError(f"Blob upload failed (HTTP {status}).")
        # 3. Publish the metadata.
        meta = _metadata_payload(doc_id, name, parent)
        status, resp = self._authed("PUT", UPDATE_STATUS_URL, json.dumps([meta]).encode())
        if status != 200:
            raise CloudError(f"Publishing metadata failed (HTTP {status}).")
        try:
            result = json.loads(resp.decode("utf-8", "replace"))[0]
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise CloudError("Metadata update returned an unexpected response.") from e
        if not result.get("Success", False):
            raise CloudError(f"Cloud rejected the upload: {result.get('Message', 'unknown reason')}")
        return meta


def token_file_mode(path: Path) -> int:
    """Permission bits of the token file (for tests/diagnostics)."""
    return stat.S_IMODE(path.stat().st_mode)
