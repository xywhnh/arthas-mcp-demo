#!/usr/bin/env python3
"""Minimal local Arthas MCP server (demo grade, no third-party deps).

Protocol: MCP over stdio, JSON-RPC 2.0, one JSON object per line.
It does NOT implement the full MCP SDK; it implements just enough for an
Agent client to list tools and call them: initialize, ping,
notifications/initialized, tools/list, tools/call.

Design choices, all deliberate for a team demo:
- Batch exec only: `kubectl exec <pod> -- bash -lc 'as.sh --select <pid> -c ...'`.
  No long-lived telnet / tunnel session to babysit.
- Compatible with the Arthas already baked into the business image
  (now 4.1.1, at /usr/app/tools/arthas, matching the real service):
  the only Arthas commands used are version / dashboard / jvm /
  thread / monitor / trace / reset, all present in 4.0.5 and 4.1.1.
  The server does NOT depend on the official (experimental)
  Arthas MCP endpoint, so no extra Arthas service is needed.
- Arthas location and Java process are server-side configuration,
  NOT per-call Agent arguments: ARTHAS_SEARCH_PATHS (colon list,
  searched in order) and ARTHAS_JAVA_PATTERN (pgrep -f pattern).
  The default search list covers the real-service layout first
  (/usr/app/tools/arthas, then the .arthas copy start-arthas.sh
  creates), then the old demo layout (/opt/arthas), so a demo
  validated here moves to the real service by changing only the
  MCP Deployment's environment, not the MCP code.
- Allowlist: only namespaces in ARTHAS_ALLOWED_NAMESPACES (default:
  demo-arthas). Anything else is rejected before kubectl runs.
- Intent-level tools, not raw Arthas passthrough: the Agent asks for
  "dashboard" / "monitor OrderService.getOrder", not for an arbitrary
  Arthas command line. There is intentionally NO ognl / redefine /
  retransform / heapdump / tt tool here.
- arthas_status is a pre-flight check, not a diagnosis: it verifies
  Pod phase/Ready, as.sh (under ARTHAS_SEARCH_PATHS), the Java pid
  (ARTHAS_JAVA_PATTERN) and the telnet binary (an as.sh prerequisite
  verified on 4.0.5; on 4.1.1 it is kept as a precaution until a
  real 4.1.1 Pod proves otherwise), plus a lightweight `version`
  attach probe. It never instruments a method.
- arthas_health_summary is a read-only composite: one call runs
  dashboard / jvm / thread and returns a structured summary with
  comparable fields. A single snapshot cannot prove a leak; compare
  two summaries (heap / Old generation) before concluding one.
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
  ARTHAS_SEARCH_PATHS        colon list, default
                             "/usr/app/tools/arthas:/usr/app/.arthas/lib/4.1.1/arthas:/opt/arthas"
  ARTHAS_JAVA_PATTERN        pgrep -f pattern, default "app.jar"
                             (real service: set this to its jar / main
                             class in the MCP Deployment; never expose
                             it as a per-call Agent argument)

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
# Server-side only (operator-set in the MCP Deployment). The Agent
# client never supplies these, so it cannot steer find/pgrep.
SEARCH_PATHS = [
    s.strip()
    for s in os.environ.get(
        "ARTHAS_SEARCH_PATHS",
        "/usr/app/tools/arthas:/usr/app/.arthas/lib/4.1.1/arthas:/opt/arthas",
    ).split(":")
    if s.strip()
]
JAVA_PATTERN = os.environ.get("ARTHAS_JAVA_PATTERN", "app.jar").strip() or "app.jar"


def _find_as() -> str:
    """Shell fragment: find the first as.sh under SEARCH_PATHS.

    Paths and the Java pattern are shlex-quoted here, so an
    operator-set value cannot break out of the inner bash command.
    """
    import shlex

    quoted = " ".join(shlex.quote(p) for p in SEARCH_PATHS)
    return f"AS=$(find {quoted} -name as.sh -type f 2>/dev/null | head -n 1); "

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
        "name": "arthas_status",
        "description": "Pre-flight check, no instrumentation: Pod phase/Ready, as.sh "
        "presence, Java pid, telnet binary (as.sh prerequisite), "
        "plus a lightweight 'version' attach probe. Call this before "
        "dashboard/trace/monitor when diagnosis fails or before a demo.",
        "inputSchema": {
            "type": "object",
            "properties": {"pod": {"type": "string"}},
        },
    },
    {
        "name": "arthas_health_summary",
        "description": "Read-only composite snapshot: runs dashboard, jvm and thread "
        "once and returns a structured summary (heap/Old/GC lines, "
        "thread states, excerpts). No instrumentation. A single "
        "snapshot cannot prove a memory leak: call it again later "
        "and compare heap/Old before concluding a leak.",
        "inputSchema": {
            "type": "object",
            "properties": {"pod": {"type": "string"}},
        },
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
        _find_as()
        + '[ -n "$AS" ] || { echo "as.sh not found under '
        + ":".join(SEARCH_PATHS)
        + '" >&2; exit 2; }; '
        f'PID=$(pgrep -f {shlex.quote(JAVA_PATTERN)} | head -n 1); '
        '[ -n "$PID" ] || { echo "java pid not found" >&2; exit 3; }; '
        f'"$AS" --select "$PID" -c {shlex.quote(arthas_cmd)}'
    )
    return _run(
        [KUBECTL, "-n", ns, "exec", pod, "--", "bash", "-lc", inner],
        timeout=timeout, allow_partial=allow_partial,
    )


def _run_rc(cmd, timeout: int):
    """Run a kubectl command, returning (returncode, combined output).

    Unlike _run, a non-zero exit is data here (e.g. pod not found),
    not an exception, so callers can report it as a status field.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "(timed out)"
    return proc.returncode, _truncate(((proc.stdout or "") + (proc.stderr or "")).strip())


def _pod_info(pod: str, ns: str) -> dict:
    rc, out = _run_rc(
        [KUBECTL, "-n", ns, "get", "pod", pod, "-o", "json"], timeout=30
    )
    if rc != 0:
        raise ValueError(f"pod not found or not readable: {pod} in {ns}: {out}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise ValueError(f"could not parse pod info for {pod}: {out[:200]}")
    statuses = data.get("status", {}).get("containerStatuses") or []
    ready = sum(1 for s in statuses if s.get("ready"))
    total = len(statuses)
    return {
        "phase": data.get("status", {}).get("phase", "unknown"),
        "ready": f"{ready}/{total}" if total else "unknown",
    }


def _preflight(pod: str, ns: str) -> dict:
    """Check as.sh / Java pid / telnet inside the Pod, without Arthas.

    The inner script always exits 0 and prints KEY=VALUE lines, so a
    missing tool is reported as a field, not as a kubectl failure.
    """
    import shlex

    inner = (
        _find_as()
        + 'echo "as.sh=${AS:-MISSING}"; '
        f'PID=$(pgrep -f {shlex.quote(JAVA_PATTERN)} | head -n 1); '
        'echo "pid=${PID:-MISSING}"; '
        'if command -v telnet >/dev/null 2>&1; then echo "telnet=ok"; '
        'else echo "telnet=MISSING"; fi; '
        'if command -v bash >/dev/null 2>&1; then echo "bash=ok"; '
        'else echo "bash=MISSING"; fi'
    )
    rc, out = _run_rc(
        [KUBECTL, "-n", ns, "exec", pod, "--", "bash", "-lc", inner], timeout=30
    )
    fields = {}
    for line in out.splitlines():
        if "=" in line:
            key, _, value = line.strip().partition("=")
            fields[key.strip()] = value.strip()
    if rc != 0 and not fields:
        raise ValueError(f"pre-flight exec failed for {pod}: {out}")
    return fields


def _status_text(pod: str, ns: str) -> str:
    info = _pod_info(pod, ns)
    pre = _preflight(pod, ns)
    as_path = pre.get("as.sh", "MISSING")
    pid = pre.get("pid", "MISSING")
    telnet = pre.get("telnet", "MISSING")
    problems = []
    if info["phase"] != "Running":
        problems.append(f"Pod phase is {info['phase']}, not Running")
    if as_path == "MISSING":
        problems.append(
            f"as.sh not found under {':'.join(SEARCH_PATHS)} "
            "(business image has no Arthas tools at the configured paths)"
        )
    if pid == "MISSING":
        problems.append(
            f"Java pid not found (no process matching {JAVA_PATTERN!r} in this Pod)"
        )
    if telnet == "MISSING":
        problems.append(
            "telnet binary missing: as.sh 4.0.5 exits 1 without it, even in "
            "-c batch mode; on 4.1.1 this is kept as a precaution until a "
            "real 4.1.1 Pod proves otherwise (if the Pod is still on a "
            "pre-telnet image, delete it so it pulls the fixed image)"
        )
    version_line = ""
    if not problems or (as_path != "MISSING" and pid != "MISSING" and telnet == "ok"):
        # Lightweight attach probe only: `version` does not instrument
        # any method, unlike monitor/trace. Skip it when a pre-flight
        # item is missing, because as.sh would fail the same way.
        try:
            version_out = _arthas(pod, "version", timeout=60, ns=ns)
            version_line = next(
                (ln.strip() for ln in version_out.splitlines() if ln.strip()), ""
            )
        except Exception as exc:  # attach failure is a status field
            problems.append(f"Arthas attach probe (version) failed: {exc}")
    lines = [
        f"pod: {pod}",
        f"namespace: {ns}",
        f"phase: {info['phase']}",
        f"ready: {info['ready']}",
        f"as.sh: {as_path}",
        f"java_pid: {pid}",
        f"telnet: {telnet}",
        f"arthas_version: {version_line or '(attach probe not run or failed)'}",
        f"status: {'ok - ready to diagnose' if not problems else 'blocked'}",
    ]
    if problems:
        lines.append("problems:")
        lines.extend(f"- {p}" for p in problems)
    return "\n".join(lines)


def _matching_lines(text: str, keywords, max_lines: int = 12) -> list:
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(kw in lowered for kw in keywords):
            if stripped not in found:
                found.append(stripped)
        if len(found) >= max_lines:
            break
    return found


def _health_summary_text(pod: str, ns: str) -> str:
    # Read-only only: dashboard / jvm / thread. No monitor/trace, so
    # this call never leaves instrumentation behind and needs no reset.
    sources = {}
    errors = {}
    for label, cmd in (
        ("dashboard", "dashboard -n 1"),
        ("jvm", "jvm"),
        ("thread", "thread"),
    ):
        try:
            sources[label] = _arthas(pod, cmd, timeout=60, ns=ns)
        except Exception as exc:
            errors[label] = str(exc)
    heap_lines = []
    for label in ("jvm", "dashboard"):
        if label in sources:
            heap_lines.extend(_matching_lines(
                sources[label],
                ("heap", "eden", "survivor", "old gen", "metaspace",
                 "non-heap", "gc", "g1"),
                max_lines=12,
            ))
    thread_text = sources.get("thread", "")
    # Count states as whole tokens, not substrings: TIMED_WAITING must
    # not also count as WAITING (underscore is a word character, so a
    # word-boundary regex handles this).
    state_counts = {}
    for state in ("BLOCKED", "RUNNABLE", "WAITING", "TIMED_WAITING"):
        count = len(re.findall(rf"(?<![A-Z_]){state}(?![A-Z_])", thread_text))
        if count:
            state_counts[state] = count
    lines = [
        f"pod: {pod}",
        f"namespace: {ns}",
        "status: " + (
            "ok - dashboard/jvm/thread all ran"
            if not errors
            else "partial - failed sources: " + ", ".join(
                f"{k} ({v[:120]})" for k, v in errors.items()
            )
        ),
        "--- heap / memory (jvm + dashboard, verbatim lines) ---",
    ]
    lines.extend(heap_lines or ["(no heap/memory lines matched; see excerpts below)"])
    lines.append("--- thread states (counts in raw thread output) ---")
    if state_counts:
        lines.extend(f"{state}: {count}" for state, count in state_counts.items())
    else:
        lines.append("(no thread state counts; see excerpt below)")
    if "dashboard" in sources:
        lines.append("--- dashboard excerpt ---")
        lines.append(sources["dashboard"][:1500])
    if "thread" in sources:
        lines.append("--- thread excerpt ---")
        lines.append(sources["thread"][:1500])
    lines.append(
        "--- note ---"
    )
    lines.append(
        "Single snapshot only. To judge a memory leak, call "
        "arthas_health_summary again after an interval (or after a "
        "leak step) and compare the heap / Old-generation lines "
        "above; a rise that does not fall after GC, with Metaspace "
        "relatively stable, points to heap retention, not Metaspace."
    )
    return _truncate("\n".join(lines))


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
    if name == "arthas_status":
        return _status_text(pod, ns)
    if name == "arthas_health_summary":
        return _health_summary_text(pod, ns)
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
