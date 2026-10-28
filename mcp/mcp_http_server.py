#!/usr/bin/env python3
"""Universal Arthas MCP server over HTTP (demo grade, stdlib only).

Why this file exists: the stdio server only works when the Agent client
runs on the same machine as kubectl. That is NOT universal -- a user on
their own laptop, with no kubectl, no Python, no kubeconfig, cannot use
it. This server fixes the client side of that problem:

  Agent client (any machine, any OS)
      --HTTP only, no kubectl, no kubeconfig, no local files-->
  MCP HTTP server (runs where kubectl CAN reach the cluster: a LAN
  server, or a Pod inside the cluster)
      --kubectl exec (server side only)-->
  App Pod -> Arthas -> JVM

Client requirements: an MCP client that can talk to a remote MCP server
over HTTP, plus the server URL (and a Bearer token, if MCP_AUTH_TOKEN
is set on the server). Nothing else. The server still enforces the
same safety rules as the stdio server: namespace allowlist
(ARTHAS_ALLOWED_NAMESPACES), the same 9 tools only (no ognl /
redefine / retransform / heapdump / tt), monitor/trace time-boxed,
output truncated to 8,000 chars.

Transports (both share arthas_mcp_server.handle_request, so the tools
are identical to the stdio server):
  * POST /mcp   -- Streamable HTTP. Send one JSON-RPC object, get one
                   JSON response back (or 202 with no body for a
                   notification). This is the transport modern clients
                   use; it needs no SSE stream for simple tool calls.
  * GET  /sse   -- Legacy SSE transport. Returns an `endpoint` event
                   with a session id; the client then POSTs JSON-RPC to
                   /messages?session_id=<id> and receives responses as
                   `message` SSE events on the GET stream.
  * GET  /health -- {"status": "ok"}; no auth required, for load
                   balancer / readiness checks only.

Tool arguments are universal: every tool accepts optional `namespace`
(must be in the server allowlist) and `app_label` (pod label value,
key is 'app'), so one server can diagnose many services, not just the
one baked into environment variables. If omitted, the server defaults
(ARTHAS_NAMESPACE / ARTHAS_APP_LABEL) apply.

Security notes, read before exposing this beyond a LAN:
  * Set MCP_AUTH_TOKEN and put the server behind a LAN-only firewall /
    NodePort / Ingress that is NOT public. The tools are read-only
    except arthas_reset (clears instrumentation) and monitor/trace
    (temporary instrumentation, always followed by reset), but an
    unauthenticated MCP server is still a remote window into your JVMs.
  * The server never logs request/response content.
  * kubectl itself is a server-side dependency only. Replacing it with
    the official Kubernetes client library later would not change the
    MCP interface the client sees.

Usage on the machine that CAN reach the cluster (e.g. the LAN server):
  ARTHAS_ALLOWED_NAMESPACES=gzdt ARTHAS_NAMESPACE=gzdt \\
  ARTHAS_APP_LABEL=arthas-demo-service MCP_AUTH_TOKEN=<a-secret> \\
  python3 mcp/mcp_http_server.py --host 0.0.0.0 --port 18080

Then from any other machine, e.g. the presenter's laptop:
  curl -s -H 'Content-Type: application/json' \\
       -H 'Authorization: Bearer <a-secret>' \\
       -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \\
       http://<lan-server-ip>:18080/mcp
"""

import argparse
import json
import os
import queue
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import arthas_mcp_server as backend  # noqa: E402  (shares tools + safety rules)

AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
SESSIONS: dict[str, queue.Queue] = {}
SESSIONS_LOCK = threading.Lock()


def _check_auth(handler: BaseHTTPRequestHandler) -> bool:
    if not AUTH_TOKEN:
        return True
    return handler.headers.get("Authorization", "").strip() == f"Bearer {AUTH_TOKEN}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout clean; never log bodies
        pass

    def _send_json(self, status: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_raw(self):
        # MUST be called for every POST, before any early return (401/404).
        # With HTTP/1.1 keep-alive, a body left unread stays in the TCP
        # stream and the next request on the same (pooled) connection is
        # then parsed as "<leftover body>POST /mcp ..." -- which is exactly
        # the "501 Unsupported method ('{...}POST')" PowerShell reported
        # after a 401: the first 401 response had not consumed its body.
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _read_json(self, raw=None):
        if raw is None:
            raw = self._read_raw()
        return json.loads(raw.decode() or "{}")

    def do_GET(self):  # noqa: N802 (http.server naming)
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if parsed.path == "/sse":
            if not _check_auth(self):
                self._send_json(401, {"error": "unauthorized"})
                return
            session_id = uuid.uuid4().hex
            q: queue.Queue = queue.Queue()
            with SESSIONS_LOCK:
                SESSIONS[session_id] = q
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(
                    f"event: endpoint\ndata: /messages?session_id={session_id}\n\n".encode()
                )
                self.wfile.flush()
                while True:
                    msg = q.get(timeout=30)
                    if msg is None:
                        break
                    self.wfile.write(
                        f"event: message\ndata: {json.dumps(msg)}\n\n".encode()
                    )
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with SESSIONS_LOCK:
                    SESSIONS.pop(session_id, None)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        # Consume the body FIRST, whatever the outcome, so keep-alive
        # connections stay in sync (see _read_raw).
        raw = self._read_raw()
        if parsed.path == "/mcp":
            if not _check_auth(self):
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                req = self._read_json(raw)
            except Exception as exc:
                self._send_json(400, {"error": f"invalid JSON: {exc}"})
                return
            resp = backend.handle_request(req)
            if resp is None:
                # Notification: Streamable HTTP accepts 202 with no body.
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_json(200, resp)
            return
        if parsed.path == "/messages":
            if not _check_auth(self):
                self._send_json(401, {"error": "unauthorized"})
                return
            session_id = parse_qs(parsed.query).get("session_id", [None])[0]
            with SESSIONS_LOCK:
                q = SESSIONS.get(session_id) if session_id else None
            if q is None:
                self._send_json(404, {"error": "unknown session"})
                return
            try:
                req = self._read_json(raw)
            except Exception as exc:
                self._send_json(400, {"error": f"invalid JSON: {exc}"})
                return
            resp = backend.handle_request(req)
            if resp is not None:
                q.put(resp)
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(404, {"error": "not found"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "18080")))
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"Arthas MCP HTTP server on http://{args.host}:{args.port}/mcp "
        f"(health: /health, legacy SSE: /sse; auth={'on' if AUTH_TOKEN else 'off (LAN only!)'})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
