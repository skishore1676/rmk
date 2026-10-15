"""Cloud push acceptance; no network — the HTTP layer is stubbed.

Covers: the upload zip layout, the metadata payload, token storage (0600,
round-trip), the register/renew/upload call sequence, and the CLI surface.
"""

import json
import stat
import uuid
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

from rmk import cloud as cloud_module
from rmk.cli import app
from rmk.cloud import CloudClient, CloudError, build_epub_zip, build_pdf_zip


def fake_pdf() -> bytes:
    # Minimal one-page PDF; pypdf counts its pages in upload_file.
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"trailer<</Root 1 0 R>>%%EOF"
    )


def test_pdf_zip_layout():
    doc_id = str(uuid.uuid4())
    blob = build_pdf_zip(doc_id, fake_pdf(), page_count=1)
    with ZipFile(BytesIO(blob)) as zf:
        names = sorted(zf.namelist())
        assert names == sorted([f"{doc_id}.content", f"{doc_id}.pagedata", f"{doc_id}.pdf"])
        content = json.loads(zf.read(f"{doc_id}.content"))
        assert content["fileType"] == "pdf"
        assert content["pageCount"] == 1
        assert zf.read(f"{doc_id}.pdf") == fake_pdf()


def test_epub_zip_layout():
    doc_id = str(uuid.uuid4())
    blob = build_epub_zip(doc_id, b"fake-epub-bytes")
    with ZipFile(BytesIO(blob)) as zf:
        assert f"{doc_id}.epub" in zf.namelist()
        assert json.loads(zf.read(f"{doc_id}.content"))["fileType"] == "epub"


def test_metadata_payload_uses_api_spelling():
    from rmk.cloud import _metadata_payload

    meta = _metadata_payload("id-1", "The Daily Suman", "")
    assert meta["VissibleName"] == "The Daily Suman"  # API spelling, double s
    assert meta["Type"] == "DocumentType"
    assert meta["Version"] == 1
    assert meta["Parent"] == ""


class FakeHTTP:
    """Stub for CloudClient.request: scripted (status, body) per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        if not self.script:
            raise AssertionError(f"unexpected HTTP call: {method} {url}")
        return self.script.pop(0)


def _client(tmp_path, script):
    return CloudClient(path=tmp_path / "cloud-tokens.json", request=FakeHTTP(script))


def test_auth_urls_live_on_current_host():
    # Regression: the auth host moved twice. The 2021 appspot host 404s, and
    # my.remarkable.com serves the website at that path (GET 200, POST 405).
    # Registration must hit webapp-prod.cloud.remarkable.engineering
    # (verified against the maintained rmapi client's config/url.go).
    assert cloud_module.DEVICE_TOKEN_URL == (
        "https://webapp-prod.cloud.remarkable.engineering/token/json/2/device/new"
    )
    assert cloud_module.USER_TOKEN_URL == (
        "https://webapp-prod.cloud.remarkable.engineering/token/json/2/user/new"
    )


def test_register_saves_tokens_with_private_perms(tmp_path):
    http = FakeHTTP([(200, b"device-token-abc"), (200, b"user-token-xyz")])
    client = CloudClient(path=tmp_path / "cloud-tokens.json", request=http)
    client.register("12345678")
    assert client.device_token == "device-token-abc"
    assert client.user_token == "user-token-xyz"
    assert client.is_authed
    # Token file exists, is private, and round-trips.
    assert stat.S_IMODE((tmp_path / "cloud-tokens.json").stat().st_mode) == 0o600
    reloaded = CloudClient(path=tmp_path / "cloud-tokens.json", request=FakeHTTP([]))
    assert reloaded.device_token == "device-token-abc"
    assert reloaded.user_token == "user-token-xyz"
    # Device registration posts the code + device description.
    method, url, body, _ = http.calls[0]
    assert method == "POST" and "device/new" in url
    assert json.loads(body)["code"] == "12345678"


def test_register_rejects_bad_code(tmp_path):
    client = _client(tmp_path, [(401, b"nope")])
    with pytest.raises(CloudError, match="registration failed"):
        client.register("bad-code")
    assert not client.is_registered


def test_renew_requires_device_token(tmp_path):
    client = _client(tmp_path, [])
    with pytest.raises(CloudError, match="No device registered"):
        client.renew()


def test_upload_sequence_new_document(tmp_path):
    put_url = "https://blob.example/upload-here"
    http = FakeHTTP([
        (200, json.dumps([{"BlobURLPut": put_url}]).encode()),
        (200, b""),
        (200, json.dumps([{"Success": True}]).encode()),
    ])
    client = _client(tmp_path, [])
    client.device_token = "d"
    client.user_token = "u"
    client._request = http
    pdf = tmp_path / "day.pdf"
    pdf.write_bytes(fake_pdf())
    meta = client.upload_file(pdf, name="The Daily Suman")
    assert meta["VissibleName"] == "The Daily Suman"
    assert meta["Version"] == 1
    # 1) upload request names the new doc, 2) blob PUT hits the signed URL,
    # 3) metadata publishes the name.
    assert http.calls[0][0] == "PUT" and "upload/request" in http.calls[0][1]
    assert http.calls[1][0] == "PUT" and http.calls[1][1] == put_url
    assert http.calls[2][0] == "PUT" and "update-status" in http.calls[2][1]
    assert json.loads(http.calls[2][2])[0]["VissibleName"] == "The Daily Suman"
    # The blob PUT carries a valid zip with the PDF inside.
    with ZipFile(BytesIO(http.calls[1][2])) as zf:
        assert any(n.endswith(".pdf") for n in zf.namelist())


def test_upload_rejects_non_pdf_epub(tmp_path):
    client = _client(tmp_path, [])
    client.device_token = "d"
    client.user_token = "u"
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    with pytest.raises(CloudError, match="Only .pdf and .epub"):
        client.upload_file(txt)


def test_upload_requires_auth(tmp_path):
    client = _client(tmp_path, [])
    pdf = tmp_path / "day.pdf"
    pdf.write_bytes(fake_pdf())
    with pytest.raises(CloudError, match="cloud-register"):
        client.upload_file(pdf)


def test_cli_push_without_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_module, "token_path", lambda: tmp_path / "cloud-tokens.json")
    runner = CliRunner()
    pdf = tmp_path / "day.pdf"
    pdf.write_bytes(fake_pdf())
    result = runner.invoke(app, ["push", str(pdf)])
    assert result.exit_code == 1
    assert "cloud-register" in result.output


def test_cli_cloud_status_unregistered(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_module, "token_path", lambda: tmp_path / "cloud-tokens.json")
    runner = CliRunner()
    result = runner.invoke(app, ["cloud-status"])
    assert result.exit_code == 1
    assert "not registered" in result.output
