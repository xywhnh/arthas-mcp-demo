#!/usr/bin/env python3
"""Minimal local Arthas MCP server (demo grade, no third-party deps).

Protocol: MCP over stdio, JSON-RPC 2.0, one JSON object per line.
It does NOT implement the full MCP SDK; it implements just enough for an
Agent client to list tools and call them: initialize, ping,
notifications/initialized, tools/list, tools/call.

Design choices, all deliberate for a team demo:
- Batch exec only: `kubectl exec <pod> -- bash -lc 'as.sh --select <pid> -c ...'`.
  No long-lived telnet / tunnel session to babysit.
- Allowlist: only namespaces in ARTHAS_ALLOWED_NAMESPACES (default:
  demo-arthas). Anything else is rejected before kubectl runs.
- Intent-level tools, not raw Arthas passthrough: the Agent asks for
  "dashboard" / "monitor OrderService.getOrder", not for an arbitrary
  Arthas command line. There is intentionally NO ognl / redefine /
  retransform / heapdump / tt tool here.
- monitor/trace are time-boxed. monitor is expected to be killed by the
  timeout; its partial output is returned, not treated as a failure.
- Outputs are truncated (8,000 chars) so a dashboard dump cannot flood
  the Agent context. No request/response content is logged.

Environment:
  ARTHAS_NAMESPACE           default "demo-arthas"
  ARTHAS_APP_LABEL           default "demo-service"
                             (gzdt: "arthas-demo-service")
  ARTHAS_ALLOWED_NAMESPACES  comma list, default "demo-arthas"
  ARTHAS_KUBECTL             default "kubectl"
  ARTHAS_TIMEOUT_SECONDS     default "65" (trace/monitor ceiling)

Before blaming this server, run scripts/arthas.sh by hand. If that fails,
fix Arthas attach first -- this server uses the same as.sh batch pattern.
"""

import json
import os
import re
import subprocess
import sys

PROTOCOL_VERSION = "2024-11-05"
MAX_CHARS = 8000

NAMESPACE = os.environ.get("ARTHAS_NAMESPACE", "demo-arthas")
# Pod label to select. kind uses app=demo-service; the LAN gzdt deployment
# renamed its objects to arthas-demo-service, so its label is
# app=arthas-demo-service. Set ARTHAS_APP_LABEL accordingly, otherwise
# list/resolve finds no pod even though the Pod is Running.
APP_LABEL = os.environ.get("ARTHAS_APP_LABEL", "demo-service")
ALLOWED = {
    s.strip()
    for s in os.environ.get("ARTHAS_ALLOWED_NAMESPACES", "demo-arthas").split(",")
    if s.strip()
}
KUBECTL = os.environ.get("ARTHAS_KUBECTL", "kubectl")
TIMEOUT = int(os.environ.get("ARTHAS_TIMEOUT_SECONDS", "65"))

POD_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\.]*$")
METHOD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

TOOLS = [
    {
        "name": "arthas_list_pods",
        "description": "List demo pods in the demo namespace (name, phase).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "arthas_dashboard",
        "description": "One Arthas dashboard snapshot (threads, memory, GC). Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"pod": {"type": "string"}},
        },
    },
    {
        "name": "arthas_jvm",
        "description": "JVM info: runtime, heap, GC, classloading. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"pod": {"type": "string"}},
        },
    },
    {
        "name": "arthas_thread",
        "description": "Thread dump summary (busiest threads first). Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"pod": {"type": "string"}},
        },
    },
    {
        "name": "arthas_monitor",
        "description": "Monitor one method for N seconds: count, success, avg/max RT. "
        "Needs traffic during the window; partial output on timeout is normal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pod": {"type": "string"},
                "class_name": {"type": "string"},
                "method": {"type": "string"},
                "seconds": {"type": "integer"},
            },
            "required": ["class_name", "method"],
        },
    },
    {
        "name": "arthas_trace",
        "description": "Trace one method until N invocations are seen, showing self vs "
        "downstream time. Needs traffic during the call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pod": {"type": "string"},
                "class_name": {"type": "string"},
                "method": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["class_name", "method"],
        },
    },
    {
        "name": "arthas_reset",
        "description": "Reset Arthas instrumentation on one Pod. Call after every trace/monitor.",
        "inputSchema": {
            "type": "object",
            "properties": {"pod": {"type": "string"}},
        },
    },
]

# Universal (non-stdio-specific) arguments, added to every tool so one MCP
# server can diagnose any allowed namespace/service, not just the one baked
# into environment variables at startup. The namespace is allowlist-checked
# in _call_tool; app_label only selects which pods to look at.
for _tool in TOOLS:
    _props = _tool["inputSchema"].setdefault("properties", {})
    _props.setdefault("namespace", {
        "type": "string",
        "description": "Kubernetes namespace to diagnose. Must be in the server allowlist "
                       "(ARTHAS_ALLOWED_NAMESPACES); defaults to the server default namespace.",
    })
    _props.setdefault("app_label", {
        "type": "string",
        "description": "Pod label value to select (label key is 'app'). Defaults to the "
                       "server default (ARTHAS_APP_LABEL).",
    })


def _check_namespace(ns: str) -> None:
    if ns not in ALLOWED:
        raise ValueError(f"namespace not allowed: {ns!r} (allowed: {sorted(ALLOWED)})")


def _check_pod(pod: str) -> None:
    if not POD_RE.match(pod):
        raise ValueError(f"invalid pod name: {pod!r}")


def _check_class_method(class_name: str, method: str) -> None:
    if not CLASS_RE.match(class_name):
        raise ValueError(f"invalid class name: {class_name!r}")
    if not METHOD_RE.match(method):
        raise ValueError(f"invalid method name: {method!r}")


def _run(cmd, timeout: int, allow_partial: bool = False) -> str:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        if allow_partial:
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            return _truncate((out + err).strip() or "(timed out, no output captured)")
        raise ValueError(f"command timed out after {timeout}s: {' '.join(cmd[:4])}")
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and not out.strip():
        raise ValueError(f"command failed (exit {proc.returncode}): {' '.join(cmd[:4])}")
    return _truncate(out.strip())


def _truncate(text: str) -> str:
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n...[truncated, {len(text)} chars total]"
    return text


def _resolve_pod(pod: str | None, ns: str, app_label: str = APP_LABEL) -> str:
    if pod:
        _check_pod(pod)
        return pod
    out = _run(
        [KUBECTL, "-n", ns, "get", "pods", "-l", f"app={app_label}",
         "-o", "jsonpath={.items[0].metadata.name}"],
        timeout=30,
    )
    if not out:
        raise ValueError(f"no pod found with label app={app_label} in namespace {ns}")
    return out


def _arthas(pod: str, arthas_cmd: str, timeout: int, allow_partial: bool = False, ns: str = NAMESPACE) -> str:
    # shlex-quote the Arthas command so a class/method name cannot break
    # out of the -c argument. The inner script finds as.sh and the Java pid.
    import shlex

    inner = (
        "AS=$(find /opt/arthas -name as.sh -type f | head -n 1); "
        '[ -n "$AS" ] || { echo "as.sh not found under /opt/arthas" >&2; exit 2; }; '
        'PID=$(pgrep -f "app.jar" | head -n 1); '
        '[ -n "$PID" ] || { echo "java pid not found" >&2; exit 3; }; '
        f'"$AS" --select "$PID" -c {shlex.quote(arthas_cmd)}'
    )
    return _run(
        [KUBECTL, "-n", ns, "exec", pod, "--", "bash", "-lc", inner],
        timeout=timeout, allow_partial=allow_partial,
    )


def _call_tool(name: str, args: dict) -> str:
    # Per-call namespace (allowlist-checked) beats the server default, so
    # one server is universal across allowed namespaces. app_label only
    # selects pods; it is not a security boundary.
    ns = (args.get("namespace") or NAMESPACE).strip()
    _check_namespace(ns)
    app_label = (args.get("app_label") or APP_LABEL).strip()
    pod = _resolve_pod(args.get("pod"), ns, app_label) if name != "arthas_list_pods" else None

    if name == "arthas_list_pods":
        out = _run(
            [KUBECTL, "-n", ns, "get", "pods", "-l", f"app={app_label}", "-o", "json"],
            timeout=30,
        )
        data = json.loads(out)
        lines = [
            f"{item['metadata']['name']} phase={item['status'].get('phase')}"
            for item in data.get("items", [])
        ]
        return "\n".join(lines) or "(no pods)"
    if name == "arthas_dashboard":
        return _arthas(pod, "dashboard -n 1", timeout=60, ns=ns)
    if name == "arthas_jvm":
        return _arthas(pod, "jvm", timeout=60, ns=ns)
    if name == "arthas_thread":
        return _arthas(pod, "thread", timeout=60, ns=ns)
    if name == "arthas_monitor":
        class_name = args["class_name"]
        method = args["method"]
        _check_class_method(class_name, method)
        seconds = int(args.get("seconds") or 10)
        seconds = max(5, min(seconds, 30))
        return _arthas(
            pod, f"monitor -c {seconds} {class_name} {method}",
            timeout=seconds + 15, allow_partial=True, ns=ns,
        )
    if name == "arthas_trace":
        class_name = args["class_name"]
        method = args["method"]
        _check_class_method(class_name, method)
        count = int(args.get("count") or 3)
        count = max(1, min(count, 10))
        return _arthas(pod, f"trace {class_name} {method} -n {count}", timeout=TIMEOUT, ns=ns)
    if name == "arthas_reset":
        return _arthas(pod, "reset", timeout=30, ns=ns)
    raise ValueError(f"unknown tool: {name}")


def _respond(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle_request(req: dict):
    """Handle one JSON-RPC request. Returns a response dict, or None for
    notifications (no id, no response). Shared by the stdio server and the
    HTTP server so both transports expose exactly the same tools."""
    method = req.get("method", "")
    msg_id = req.get("id")
    if method.startswith("notifications/"):
        return None
    if msg_id is None:
        return None
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "arthas-mcp-demo", "version": "0.1.0"},
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = req.get("params", {})
            text = _call_tool(params.get("name", ""), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": text}],
            }}
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"}}
    except Exception as exc:  # keep the server alive for the next call
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": f"error: {exc}"}],
                "isError": True,
            }}
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32603, "message": str(exc)}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
