"""Wire-level MCP tests without a tablet, notebook config, or cloud account."""

import base64
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest

from rmk.mcp_server import (
    CAPABILITIES_META, PROTOCOL_META, PROTOCOL_VERSION, SERVER_INFO_META,
    SUPPORTED_PROTOCOL_VERSIONS, create_http_server,
)


TOKEN = "synthetic-test-token-with-at-least-32-characters"
SECRET = "sensitive-token-or-notebook-text-must-never-escape"
ACCEPT = "application/json, text/event-stream"


def request(method="ping", *, request_id=1, params=None, modern=True):
    params = dict(params or {})
    if modern:
        params["_meta"] = {PROTOCOL_META: PROTOCOL_VERSION, CAPABILITIES_META: {}}
    result = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        result["id"] = request_id
    return result


class FakeService:
    tools = [
        {"name": name, "description": name,
         "inputSchema": {"type": "object", "properties": {}}}
        for name in ("status", "write", "fail", "invalid", "café")
    ]

    def __init__(self):
        self.calls = []
        self.writes = 0

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "fail":
            raise RuntimeError(SECRET)
        if name == "invalid":
            return {"content": SECRET}
        if name == "write":
            self.writes += 1
        return {"content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"writes": self.writes}}


def stdio_process(messages, *, fake=True, limit=None):
    env = dict(os.environ)
    source = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join((str(source), str(Path(__file__).parent), env.get("PYTHONPATH", "")))
    env.pop("RMK_MCP_TOKEN", None)
    if fake:
        script = ("from test_mcp_server import FakeService\n"
                  "from rmk.mcp_server import run_stdio\n"
                  f"run_stdio(FakeService(){', max_message_bytes=' + str(limit) if limit else ''})\n")
        command = [sys.executable, "-c", script]
    else:
        command = [sys.executable, "-m", "rmk.mcp_server"]
    raw = b"".join(item if isinstance(item, bytes) else json.dumps(item).encode() + b"\n"
                   for item in messages)
    result = subprocess.run(command, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, timeout=10)
    return result, [json.loads(line) for line in result.stdout.splitlines()]


def test_subprocess_discovery_and_ping_do_not_require_companion_configuration():
    process, replies = stdio_process([
        request("server/discover"),
        request("initialize", request_id=2, modern=False, params={
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        }),
        request("notifications/initialized", request_id=None, modern=False),
        request(request_id=3, modern=False),
    ], fake=False)
    assert process.returncode == 0
    assert process.stderr == b""
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    discovery = replies[0]["result"]
    assert discovery["resultType"] == "complete"
    assert discovery["supportedVersions"] == list(SUPPORTED_PROTOCOL_VERSIONS)
    assert discovery["capabilities"] == {"tools": {}}
    assert discovery["_meta"][SERVER_INFO_META]["name"] == "remarkable-companion"
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert "resultType" not in replies[1]["result"]
    assert replies[2]["result"] == {}


def test_subprocess_lists_tools_and_ignores_call_notifications_without_effects():
    process, replies = stdio_process([
        request("tools/list"),
        request("tools/call", request_id=None, params={"name": "write"}),
        request("tools/call", request_id=2, params={"name": "status", "arguments": {}}),
        request("tools/call", request_id=3, params={"name": "write", "arguments": {}}),
    ])
    assert process.returncode == 0 and process.stderr == b""
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert [tool["name"] for tool in replies[0]["result"]["tools"]] == [tool["name"] for tool in FakeService.tools]
    assert replies[1]["result"]["structuredContent"] == {"writes": 0}
    assert replies[2]["result"]["structuredContent"] == {"writes": 1}


def test_subprocess_recovers_from_bad_json_and_bounded_oversize_lines():
    process, replies = stdio_process([
        b"not-json\n",
        b'{"jsonrpc":"2.0","id":0,"method":"ping","params":{"bad":NaN}}\n',
        b'{"jsonrpc":"2.0","id":0,"method":"ping","method":"tools/call"}\n',
        b"x" * 1100 + b"\n",
        request(request_id="after", modern=False),
    ], limit=512)
    assert process.returncode == 0 and process.stderr == b""
    assert [reply["error"]["code"] for reply in replies[:-1]] == [-32700, -32700, -32700, -32600]
    assert replies[-1] == {"jsonrpc": "2.0", "id": "after", "result": {}}


def test_subprocess_suppresses_sensitive_exception_details():
    process, replies = stdio_process([
        request("tools/call", params={"name": "fail"}),
        request("tools/call", request_id=2, params={"name": "invalid"}),
        request(request_id=3),
    ])
    assert process.returncode == 0
    assert SECRET.encode() not in process.stdout + process.stderr
    assert process.stderr == b"rmk-mcp: internal error\n" * 2
    assert [reply["error"]["code"] for reply in replies[:2]] == [-32603, -32603]
    assert replies[2]["result"]["resultType"] == "complete"


def test_subprocess_handles_escaped_unicode_request_ids():
    process, replies = stdio_process([request(request_id="\ud800", modern=False)])
    assert process.returncode == 0 and process.stderr == b""
    assert replies == [{"jsonrpc": "2.0", "id": "\ud800", "result": {}}]


@pytest.mark.parametrize("version", ["2025-03-26", "2025-06-18", "2025-11-25", "1900-01-01"])
def test_subprocess_legacy_version_negotiation(version):
    process, replies = stdio_process([request("initialize", modern=False, params={
        "protocolVersion": version, "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    })], fake=False)
    assert process.returncode == 0 and process.stderr == b""
    assert replies[0]["result"]["protocolVersion"] == (version if version != "1900-01-01" else "2025-11-25")


@contextmanager
def running_http(*, limit=20 * 1024 * 1024):
    service = FakeService()
    server = create_http_server(service, port=0, token=TOKEN, max_body_bytes=limit)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, service
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def http_server():
    with running_http() as pair:
        yield pair


def http_request(server, message=None, *, method="POST", path="/mcp", headers=None):
    defaults = {"Authorization": "Bearer " + TOKEN, "Accept": ACCEPT, "Content-Type": "application/json"}
    if isinstance(message, dict):
        metadata = message.get("params", {}).get("_meta", {}) if isinstance(message.get("params", {}), dict) else {}
        if PROTOCOL_META in metadata:
            defaults["MCP-Protocol-Version"] = metadata[PROTOCOL_META]
            defaults["Mcp-Method"] = message["method"]
            if message["method"] == "tools/call" and isinstance(message["params"].get("name"), str):
                name = message["params"]["name"]
                defaults["Mcp-Name"] = name if name.isascii() else "=?base64?" + base64.b64encode(name.encode()).decode() + "?="
        body = json.dumps(message).encode()
    else:
        body = message
    for name, value in (headers or {}).items():
        if value is None:
            defaults.pop(name, None)
        else:
            defaults[name] = value
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=defaults)
        response = connection.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), json.loads(data) if data else None
    finally:
        connection.close()


def test_http_discovery_and_tool_results_on_loopback(http_server):
    server, service = http_server
    assert server.server_address[0] == "127.0.0.1"
    status, headers, result = http_request(server, request("server/discover"))
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert result["result"]["resultType"] == "complete"
    assert service.calls == []
    assert "Mcp-Session-Id" not in headers
    status, _, result = http_request(server, request("tools/call", params={"name": "status"}))
    assert status == 200
    assert result["result"]["structuredContent"] == {"writes": 0}
    assert service.calls == [("status", {})]


def test_http_notification_has_empty_202_and_never_calls_service(http_server):
    server, service = http_server
    for notification in [request("notifications/initialized", request_id=None, modern=False),
                         request("tools/call", request_id=None, params={"name": "write"})]:
        status, headers, body = http_request(server, notification)
        assert status == 202 and body is None and headers["Content-Length"] == "0"
    assert service.calls == [] and service.writes == 0
    invalid = {"jsonrpc": "2.0", "method": "tools/call", "params": []}
    status, _, body = http_request(server, invalid)
    assert status == 400 and body is None
    assert service.calls == []


@pytest.mark.parametrize("headers,status", [
    ({"Authorization": None}, 401),
    ({"Authorization": "Bearer wrong"}, 401),
    ({"Origin": "https://hostile.example"}, 403),
    ({"Origin": "null"}, 403),
    ({"Host": "hostile.example"}, 403),
    ({"Content-Type": "text/plain"}, 415),
    ({"Content-Type": "application/json; charset=latin-1"}, 415),
    ({"Content-Encoding": "gzip"}, 415),
    ({"Accept": "text/html"}, 406),
    ({"Transfer-Encoding": "chunked"}, 400),
    ({"Content-Length": "-1"}, 400),
    ({"Content-Length": "many"}, 400),
])
def test_http_rejects_invalid_access_and_framing_before_tool_execution(http_server, headers, status):
    server, service = http_server
    actual, _, _ = http_request(server, request("tools/call", params={"name": "write"}), headers=headers)
    assert actual == status and service.calls == []


def test_http_accepts_exact_loopback_origin(http_server):
    server, _ = http_server
    status, _, _ = http_request(server, request(), headers={"Origin": f"http://127.0.0.1:{server.server_port}"})
    assert status == 200


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"])
def test_http_only_post_is_supported(http_server, method):
    server, service = http_server
    status, headers, body = http_request(server, method=method)
    assert status == 405 and headers["Allow"] == "POST" and body is None
    assert service.calls == []


def test_http_missing_route_does_not_dispatch(http_server):
    server, service = http_server
    status, _, _ = http_request(server, request("tools/call", params={"name": "write"}), path="/other")
    assert status == 404 and service.calls == []


def test_http_rejects_oversized_body_before_reading_or_dispatching():
    with running_http(limit=64) as (server, service):
        status, _, _ = http_request(server, b"{}", headers={"Content-Length": "65"})
        assert status == 413 and service.calls == []


@pytest.mark.parametrize("extra,expected", [
    ("", 411),
    ("Content-Length: 0\r\nContent-Length: 0\r\n", 400),
    ("Content-Length: 0\r\nAuthorization: Bearer another\r\n", 400),
    ("Content-Length: 0\r\nHost: hostile.example\r\n", 400),
])
def test_http_rejects_missing_or_duplicate_security_headers(http_server, extra, expected):
    server, service = http_server
    raw = (f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
           f"Authorization: Bearer {TOKEN}\r\nAccept: {ACCEPT}\r\n"
           f"Content-Type: application/json\r\n{extra}\r\n").encode()
    with socket.create_connection(server.server_address, timeout=3) as connection:
        connection.sendall(raw)
        response = connection.recv(4096)
    assert response.startswith(f"HTTP/1.1 {expected} ".encode())
    assert service.calls == []


@pytest.mark.parametrize("raw,code", [
    (b"not-json", -32700),
    (b"\xff", -32700),
    (b"[]", -32600),
    (b"null", -32600),
    (b'{"jsonrpc":"2.0","id":null,"method":"ping"}', -32600),
    (b'{"jsonrpc":"2.0","id":true,"method":"ping"}', -32600),
    (b'{"jsonrpc":"2.0","id":1.2,"method":"ping"}', -32600),
    (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":[]}', -32602),
    (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":null}', -32602),
])
def test_http_malformed_json_and_envelopes(http_server, raw, code):
    server, service = http_server
    status, _, body = http_request(server, raw)
    assert status == 400 and body["error"]["code"] == code
    assert service.calls == []


@pytest.mark.parametrize("method,params,code,status", [
    ("unknown", {}, -32601, 404),
    ("tools/list", {"cursor": "missing"}, -32602, 400),
    ("tools/call", {"name": "missing"}, -32602, 400),
    ("tools/call", {"name": "write", "arguments": []}, -32602, 400),
    ("initialize", {}, -32602, 400),
])
def test_http_method_and_parameter_errors(http_server, method, params, code, status):
    server, service = http_server
    actual, _, body = http_request(server, request(method, params=params, modern=method != "initialize"))
    assert actual == status and body["error"]["code"] == code
    assert service.calls == []


@pytest.mark.parametrize("headers", [
    {"MCP-Protocol-Version": None},
    {"MCP-Protocol-Version": "2025-11-25"},
    {"Mcp-Method": None},
    {"Mcp-Method": "tools/list"},
    {"Mcp-Name": None},
    {"Mcp-Name": "status"},
    {"Mcp-Name": "=?base64?invalid!?="},
])
def test_http_modern_headers_must_match_body(http_server, headers):
    server, service = http_server
    status, _, body = http_request(server, request("tools/call", params={"name": "write"}), headers=headers)
    assert status == 400 and body["error"]["code"] == -32020
    assert service.calls == []


def test_http_decodes_base64_name_header_before_comparison(http_server):
    server, service = http_server
    status, _, _ = http_request(server, request("tools/call", params={"name": "café"}))
    assert status == 200 and service.calls == [("café", {})]


def test_http_modern_metadata_and_version_errors(http_server):
    server, service = http_server
    missing = request()
    del missing["params"]["_meta"][CAPABILITIES_META]
    status, _, body = http_request(server, missing)
    assert status == 400 and body["error"]["code"] == -32602
    invalid_info = request()
    invalid_info["params"]["_meta"]["io.modelcontextprotocol/clientInfo"] = {"name": "test"}
    status, _, body = http_request(server, invalid_info)
    assert status == 400 and body["error"]["code"] == -32602
    unsupported = request()
    unsupported["params"]["_meta"][PROTOCOL_META] = "1900-01-01"
    status, _, body = http_request(server, unsupported)
    assert status == 400 and body["error"]["code"] == -32022
    assert body["error"]["data"] == {"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": "1900-01-01"}
    assert service.calls == []


def test_http_legacy_initialize_and_header_version(http_server):
    server, _ = http_server
    status, _, body = http_request(server, request("initialize", modern=False, params={
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    }))
    assert status == 200 and body["result"]["protocolVersion"] == "2025-11-25"
    status, _, body = http_request(server, request("tools/list", modern=False),
                                   headers={"MCP-Protocol-Version": "2025-11-25"})
    assert status == 200 and "resultType" not in body["result"]
    status, _, body = http_request(server, request(modern=False), headers={"MCP-Protocol-Version": "1900-01-01"})
    assert status == 400 and body["error"]["code"] == -32022


def test_http_unexpected_errors_are_generic(http_server, capsys):
    server, _ = http_server
    status, _, body = http_request(server, request("tools/call", params={"name": "fail"}))
    assert status == 500 and body["error"] == {"code": -32603, "message": "Internal error"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "rmk-mcp: internal error\n"
    assert SECRET not in json.dumps(body) + captured.err


@pytest.mark.parametrize("token", ["", "short", "a" * 31, "a" * 31 + " ", "é" * 32])
def test_http_cannot_start_with_missing_or_weak_token(token):
    with pytest.raises(ValueError, match="RMK_MCP_TOKEN"):
        create_http_server(FakeService(), port=0, token=token)


def test_http_reads_token_from_environment(monkeypatch):
    monkeypatch.setenv("RMK_MCP_TOKEN", TOKEN)
    server = create_http_server(FakeService(), port=0)
    server.server_close()
    monkeypatch.delenv("RMK_MCP_TOKEN")
    with pytest.raises(ValueError, match="RMK_MCP_TOKEN"):
        create_http_server(FakeService(), port=0)
