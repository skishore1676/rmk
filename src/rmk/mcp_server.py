"""Small, dependency-free MCP transport for the reMarkable companion.

The default is newline-delimited JSON-RPC over stdio.  The optional HTTP
transport is deliberately a local endpoint: it cannot bind to a public
interface, requires a bearer token, and does not provide an SSE subscription
stream.  Discovery is independent of notebook configuration.

Both the 2026-07-28 per-request protocol and the 2025 initialize-based
protocols are supported.  Notebook behavior belongs to ``Companion``; this
module only validates the protocol envelope and transports its tool results.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import hmac
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO


PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION, *LEGACY_PROTOCOL_VERSIONS)
PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
SERVER_INFO = {"name": "remarkable-companion", "version": "0.2.0"}
CAPABILITIES: dict[str, Any] = {"tools": {}}
MAX_MESSAGE_BYTES = 20 * 1024 * 1024
DEFAULT_PORT = 8793
_MISSING = object()


class ProtocolError(Exception):
    """A deliberately non-sensitive error suitable for a JSON-RPC response."""

    def __init__(self, code: int, message: str, *, status: int = 400,
                 data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.data = data


def _error(request_id: Any, code: int, message: str,
           data: dict[str, Any] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        value["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": value}


def _internal_error() -> None:
    # Exceptions can contain tokens, paths, or notebook text.  Never print them.
    print("rmk-mcp: internal error", file=sys.stderr)


def _json_bytes(value: Any) -> bytes:
    # Escaping also preserves an otherwise valid JSON string containing a lone
    # surrogate, without letting it break UTF-8 response serialization.
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ProtocolError(-32700, "Parse error") from None


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) or type(value) is int


def _decoded_header(value: str) -> str:
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            raise ProtocolError(-32020, "Required MCP headers are missing or do not match") from None
    if value != value.strip() or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ProtocolError(-32020, "Required MCP headers are missing or do not match")
    return value


class Dispatcher:
    """One service instance, serialized across concurrent HTTP connections."""

    def __init__(self, service: Any = None):
        self._service = service
        self._lock = threading.RLock()
        self.legacy_version = LEGACY_PROTOCOL_VERSIONS[0]

    @property
    def service(self) -> Any:
        if self._service is None:
            from rmk.companion import Companion
            self._service = Companion()
        return self._service

    def handle(self, message: Any, *, headers: dict[str, str] | None = None
               ) -> tuple[dict[str, Any] | None, int]:
        request_id = None
        notification = False
        try:
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ProtocolError(-32600, "Invalid request")
            if "id" in message:
                if not _valid_id(message["id"]):
                    raise ProtocolError(-32600, "Invalid request ID")
                request_id = message["id"]
            notification = "id" not in message and isinstance(message.get("method"), str)
            method = message.get("method")
            if not isinstance(method, str) or not method or "result" in message or "error" in message:
                raise ProtocolError(-32600, "Invalid request")
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ProtocolError(-32602, "Params must be an object")
            # Notifications have no request/reply contract.  In particular, a
            # tools/call notification must never become a fire-and-forget write.
            if notification:
                return None, 202
            with self._lock:
                version = self._version(method, params, headers)
                result = self._invoke(method, params)
                if version == PROTOCOL_VERSION:
                    result = copy.deepcopy(result)
                    result["resultType"] = "complete"
                    metadata = result.setdefault("_meta", {})
                    if not isinstance(metadata, dict):
                        raise TypeError("Invalid result metadata")
                    metadata[SERVER_INFO_META] = dict(SERVER_INFO)
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
                _json_bytes(response)  # Check output before touching the wire.
                return response, 200
        except ProtocolError as error:
            if notification:
                return None, error.status
            return _error(request_id, error.code, error.message, error.data), error.status
        except Exception:
            _internal_error()
            if notification:
                return None, 500
            return _error(request_id, -32603, "Internal error"), 500

    def _version(self, method: str, params: dict[str, Any],
                 headers: dict[str, str] | None) -> str:
        metadata = params.get("_meta", {})
        if not isinstance(metadata, dict):
            raise ProtocolError(-32602, "Request metadata must be an object")
        body_version = metadata.get(PROTOCOL_META, _MISSING)
        header_version = headers.get("mcp-protocol-version") if headers is not None else None
        per_request = (body_version is not _MISSING or CAPABILITIES_META in metadata
                       or method == "server/discover" or header_version == PROTOCOL_VERSION)
        if per_request:
            if not isinstance(body_version, str) or not isinstance(metadata.get(CAPABILITIES_META), dict):
                raise ProtocolError(-32602, "Protocol version and client capabilities are required in request metadata")
            if "io.modelcontextprotocol/clientInfo" in metadata:
                info = metadata["io.modelcontextprotocol/clientInfo"]
                if (not isinstance(info, dict) or not isinstance(info.get("name"), str)
                        or not isinstance(info.get("version"), str)):
                    raise ProtocolError(-32602, "Invalid client information in request metadata")
            version = body_version
        else:
            version = header_version or ("2025-03-26" if headers is not None else self.legacy_version)
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ProtocolError(-32022, "Unsupported protocol version", data={
                "supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": version,
            })
        if headers is not None:
            modern = version == PROTOCOL_VERSION
            expected = {"mcp-protocol-version": version, "mcp-method": method}
            if method in ("tools/call", "prompts/get", "resources/read"):
                expected["mcp-name"] = params.get("uri" if method == "resources/read" else "name")
            for key, value in expected.items():
                actual = headers.get(key)
                if actual is None and not modern:
                    continue
                if actual is not None and key == "mcp-name":
                    actual = _decoded_header(actual)
                if actual != value:
                    raise ProtocolError(-32020, "Required MCP headers are missing or do not match")
        if method == "server/discover" and version != PROTOCOL_VERSION:
            raise ProtocolError(-32601, "Method not found for this protocol version", status=404)
        if method == "initialize" and version == PROTOCOL_VERSION:
            raise ProtocolError(-32601, "Use server/discover for this protocol version", status=404)
        return version

    def _invoke(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "server/discover":
            return {"supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                    "capabilities": copy.deepcopy(CAPABILITIES)}
        if method == "initialize":
            info = params.get("clientInfo")
            requested = params.get("protocolVersion")
            if (not isinstance(requested, str) or not isinstance(params.get("capabilities"), dict)
                    or not isinstance(info, dict) or not isinstance(info.get("name"), str)
                    or not isinstance(info.get("version"), str)):
                raise ProtocolError(-32602, "Invalid initialize parameters")
            self.legacy_version = requested if requested in LEGACY_PROTOCOL_VERSIONS else LEGACY_PROTOCOL_VERSIONS[0]
            return {"protocolVersion": self.legacy_version, "capabilities": copy.deepcopy(CAPABILITIES),
                    "serverInfo": dict(SERVER_INFO)}
        if method == "ping":
            return {}
        if method == "tools/list":
            if "cursor" in params:
                raise ProtocolError(-32602, "Pagination cursor is not supported")
            return {"tools": copy.deepcopy(self.service.tools)}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                raise ProtocolError(-32602, "Tool name and an arguments object are required")
            if not any(tool.get("name") == name for tool in self.service.tools):
                raise ProtocolError(-32602, "Unknown tool")
            result = self.service.call_tool(name, arguments)
            if (not isinstance(result, dict) or not isinstance(result.get("content"), list)
                    or ("structuredContent" in result and not isinstance(result["structuredContent"], dict))
                    or ("isError" in result and not isinstance(result["isError"], bool))):
                raise TypeError("Invalid tool result")
            return result
        raise ProtocolError(-32601, "Method not found", status=404)


def run_stdio(service: Any = None, *, input_stream: BinaryIO | None = None,
              output_stream: BinaryIO | None = None,
              max_message_bytes: int = MAX_MESSAGE_BYTES) -> None:
    """Read bounded UTF-8 JSON lines; write responses only to stdout."""
    if type(max_message_bytes) is not int or max_message_bytes < 1:
        raise ValueError("Message limit must be a positive integer")
    source = input_stream if input_stream is not None else sys.stdin.buffer
    target = output_stream if output_stream is not None else sys.stdout.buffer
    dispatcher = Dispatcher(service)
    while True:
        raw = source.readline(max_message_bytes + 2)
        if not raw:
            break
        content = raw[:-1] if raw.endswith(b"\n") else raw
        if len(content) > max_message_bytes:
            while raw and not raw.endswith(b"\n"):
                raw = source.readline(max_message_bytes + 2)
            response = _error(None, -32600, "Message exceeds size limit")
        else:
            try:
                message = _parse(content)
                response, _ = dispatcher.handle(message)
            except ProtocolError as error:
                response = _error(None, error.code, error.message)
        if response is not None:
            target.write(_json_bytes(response) + b"\n")
            target.flush()


class _LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        _internal_error()


def create_http_server(service: Any = None, *, port: int = DEFAULT_PORT,
                       token: str | None = None,
                       max_body_bytes: int = MAX_MESSAGE_BYTES) -> ThreadingHTTPServer:
    """Create, but do not start, an authenticated 127.0.0.1-only MCP server."""
    token = os.environ.get("RMK_MCP_TOKEN", "") if token is None else token
    if (not isinstance(token, str) or len(token) < 32
            or any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise ValueError("RMK_MCP_TOKEN must contain at least 32 printable ASCII characters without spaces")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535")
    if type(max_body_bytes) is not int or max_body_bytes < 1:
        raise ValueError("Body limit must be a positive integer")
    expected_authorization = ("Bearer " + token).encode("ascii")
    dispatcher = Dispatcher(service)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "rmk-mcp"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, status: int, payload: dict[str, Any] | None = None) -> None:
            body = b"" if payload is None else _json_bytes(payload)
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            if status == 405:
                self.send_header("Allow", "POST")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Bearer realm="rmk-mcp"')
            self.end_headers()
            self.close_connection = True
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def send_error(self, code: int, message: str | None = None,
                       explain: str | None = None) -> None:
            self._send(code)

        def handle_expect_100(self) -> bool:
            self._send(417)
            return False

        def _access(self) -> bool:
            for name in ("Host", "Origin", "Authorization", "Content-Length", "Content-Type",
                         "MCP-Protocol-Version", "Mcp-Method", "Mcp-Name"):
                values = self.headers.get_all(name, [])
                if len(values) > 1 or any("\r" in value or "\n" in value for value in values):
                    self._send(400)
                    return False
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != host:
                self._send(403)
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + host:
                self._send(403)
                return False
            authorization = self.headers.get("Authorization", "").encode("utf-8")
            if not hmac.compare_digest(authorization, expected_authorization):
                self._send(401)
                return False
            if self.path != "/mcp":
                self._send(404)
                return False
            return True

        def do_POST(self) -> None:
            if not self._access():
                return
            if self.headers.get("Transfer-Encoding") is not None:
                self._send(400)
                return
            if self.headers.get("Content-Encoding", "identity").lower() != "identity":
                self._send(415)
                return
            if self.headers.get("Expect") is not None:
                self._send(417)
                return
            if self.headers.get_content_type() != "application/json":
                self._send(415)
                return
            charset = self.headers.get_content_charset()
            if charset is not None and charset.lower() not in ("utf-8", "utf8"):
                self._send(415)
                return
            accept = self.headers.get("Accept", "")
            accepted = {part.strip().split(";", 1)[0].lower() for part in accept.split(",")}
            if not {"application/json", "text/event-stream"}.issubset(accepted):
                self._send(406)
                return
            length = self.headers.get("Content-Length")
            if length is None:
                self._send(411)
                return
            if not re.fullmatch(r"[0-9]+", length) or len(length) > 12:
                self._send(400)
                return
            size = int(length)
            if size > max_body_bytes:
                self._send(413)
                return
            try:
                raw = self.rfile.read(size)
                if len(raw) != size:
                    self._send(400)
                    return
                message = _parse(raw)
            except (OSError, TimeoutError):
                self._send(408)
                return
            except ProtocolError as error:
                self._send(error.status, _error(None, error.code, error.message))
                return
            headers = {key.lower(): value for key, value in self.headers.items()}
            response, status = dispatcher.handle(message, headers=headers)
            self._send(status, response)

        def _method_not_allowed(self) -> None:
            if self._access():
                self._send(405)

        def __getattr__(self, name: str) -> Any:
            if name.startswith("do_"):
                return self._method_not_allowed
            raise AttributeError(name)

    return _LocalHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="reMarkable companion MCP server")
    parser.add_argument("--http", action="store_true", help="Serve authenticated HTTP on 127.0.0.1 instead of stdio")
    parser.add_argument("--port", type=int, help=f"Local HTTP port (default: {DEFAULT_PORT})")
    args = parser.parse_args(argv)
    if args.port is not None and not args.http:
        parser.error("--port requires --http")
    try:
        if args.http:
            server = create_http_server(port=DEFAULT_PORT if args.port is None else args.port)
            try:
                server.serve_forever()
            finally:
                server.server_close()
        else:
            run_stdio()
    except ValueError as error:
        # Startup validation errors here use fixed messages, never token values.
        parser.error(str(error))
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    except Exception:
        _internal_error()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
