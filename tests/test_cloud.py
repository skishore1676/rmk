"""Cloud push acceptance; no network — the HTTP layer and rmapi are stubbed.

Covers: token storage (0600, round-trip), the register/renew call sequence,
the rmapi upload delegate (argv, name override, folder), ``rmapi ls``
parsing, and the CLI surface.
"""

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from rmk import cloud as cloud_module
from rmk.cli import app
from rmk.cloud import CloudClient, CloudError


def fake_pdf() -> bytes:
    # Minimal one-page PDF.
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"trailer<</Root 1 0 R>>%%EOF"
    )


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


class FakeRunner:
    """Stub for CloudClient.runner: scripted rmapi results per call."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.put_contents = []  # bytes of each file handed to `rmapi put`

    def __call__(self, argv):
        self.calls.append(argv)
        if len(argv) >= 3 and argv[1] == "put":
            self.put_contents.append(Path(argv[2]).read_bytes())
        if not self.results:
            raise AssertionError(f"unexpected rmapi call: {argv}")
        rc, out, err = self.results.pop(0)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _client(tmp_path, script=(), runner=None):
    return CloudClient(
        path=tmp_path / "cloud-tokens.json", request=FakeHTTP(script), runner=runner
    )


def _authed_client(tmp_path, runner, monkeypatch):
    client = _client(tmp_path, runner=runner)
    client.device_token = "d"
    client.user_token = "u"
    monkeypatch.setattr(cloud_module, "find_rmapi", lambda: Path("/bin/rmapi"))
    monkeypatch.setattr(cloud_module, "_rmapi_registered", lambda: True)
    return client


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


def test_upload_delegates_to_rmapi_put(tmp_path, monkeypatch):
    runner = FakeRunner([(0, "uploading...OK\n", "")])
    client = _authed_client(tmp_path, runner, monkeypatch)
    pdf = tmp_path / "day.pdf"
    pdf.write_bytes(fake_pdf())
    meta = client.upload_file(pdf)
    assert meta["name"] == "day"
    assert runner.calls == [["/bin/rmapi", "put", str(pdf)]]


def test_upload_name_override_stages_renamed_copy(tmp_path, monkeypatch):
    runner = FakeRunner([(0, "uploading...OK\n", "")])
    client = _authed_client(tmp_path, runner, monkeypatch)
    pdf = tmp_path / "day.pdf"
    pdf.write_bytes(fake_pdf())
    meta = client.upload_file(pdf, name="The Daily Suman")
    assert meta["name"] == "The Daily Suman"
    staged = runner.calls[0][2]
    assert staged.endswith("The Daily Suman.pdf")
    assert runner.put_contents == [fake_pdf()]


def test_upload_parent_passes_folder(tmp_path, monkeypatch):
    runner = FakeRunner([(0, "uploading...OK\n", "")])
    client = _authed_client(tmp_path, runner, monkeypatch)
    pdf = tmp_path / "day.pdf"
    pdf.write_bytes(fake_pdf())
    client.upload_file(pdf, parent="/Personal")
    assert runner.calls == [["/bin/rmapi", "put", str(pdf), "/Personal"]]


def test_upload_rejects_non_pdf_epub(tmp_path, monkeypatch):
    runner = FakeRunner([])
    client = _authed_client(tmp_path, runner, monkeypatch)
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    with pytest.raises(CloudError, match="Only .pdf and .epub"):
        client.upload_file(txt)
    assert runner.calls == []


def test_upload_requires_auth(tmp_path, monkeypatch):
    runner = FakeRunner([])
    client = _client(tmp_path, runner=runner)
    monkeypatch.setattr(cloud_module, "find_rmapi", lambda: Path("/bin/rmapi"))
    monkeypatch.setattr(cloud_module, "_rmapi_registered", lambda: True)
    pdf = tmp_path / "day.pdf"
    pdf.write_bytes(fake_pdf())
    with pytest.raises(CloudError, match="cloud-register"):
        client.upload_file(pdf)
    assert runner.calls == []


def test_upload_requires_rmapi_registered(tmp_path, monkeypatch):
    runner = FakeRunner([])
    client = _client(tmp_path, runner=runner)
    client.device_token = "d"
    client.user_token = "u"
    monkeypatch.setattr(cloud_module, "find_rmapi", lambda: Path("/bin/rmapi"))
    monkeypatch.setattr(cloud_module, "_rmapi_registered", lambda: False)
    pdf = tmp_path / "day.pdf"
    pdf.write_bytes(fake_pdf())
    with pytest.raises(CloudError, match="rmapi is not registered"):
        client.upload_file(pdf)
    assert runner.calls == []


def test_upload_reports_rmapi_failure(tmp_path, monkeypatch):
    runner = FakeRunner([(1, "", "boom\n")])
    client = _authed_client(tmp_path, runner, monkeypatch)
    pdf = tmp_path / "day.pdf"
    pdf.write_bytes(fake_pdf())
    with pytest.raises(CloudError, match="rmapi put .* failed: boom"):
        client.upload_file(pdf)


def test_list_documents_parses_rmapi_ls(tmp_path, monkeypatch):
    runner = FakeRunner([(0, "[f]\tCalendar\n[d]\tPersonal\n", "")])
    client = _client(tmp_path, runner=runner)
    monkeypatch.setattr(cloud_module, "find_rmapi", lambda: Path("/bin/rmapi"))
    monkeypatch.setattr(cloud_module, "_rmapi_registered", lambda: True)
    docs = client.list_documents()
    assert docs == [
        {"kind": "[f]", "name": "Calendar"},
        {"kind": "[d]", "name": "Personal"},
    ]
    assert runner.calls == [["/bin/rmapi", "ls"]]


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
