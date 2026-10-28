#!/usr/bin/env bash
# Direct Arthas check WITHOUT the MCP server. Run this first: if this fails,
# fix Arthas attach before blaming the MCP layer.
#
# Usage (kind defaults; gzdt: NS=gzdt APP_LABEL=arthas-demo-service):
#   scripts/arthas.sh dashboard
#   NS=gzdt APP_LABEL=arthas-demo-service scripts/arthas.sh dashboard
#   scripts/arthas.sh jvm
#   scripts/arthas.sh 'trace com.example.demo.order.OrderService getOrder -n 3'
# For trace/monitor, generate traffic in another terminal while it runs.
#
# Arthas location / Java process are server-side configuration here too,
# mirroring mcp/arthas_mcp_server.py:
#   ARTHAS_SEARCH_PATHS  default "/usr/app/tools/arthas:/usr/app/.arthas/lib/4.1.1/arthas:/opt/arthas"
#   ARTHAS_JAVA_PATTERN  default "app.jar"
set -euo pipefail
NS="${NS:-demo-arthas}"
APP_LABEL="${APP_LABEL:-demo-service}"
POD="${POD:-$(kubectl -n "$NS" get pods -l app="$APP_LABEL" -o jsonpath='{.items[0].metadata.name}')}"
SEARCH_PATHS="${ARTHAS_SEARCH_PATHS:-/usr/app/tools/arthas:/usr/app/.arthas/lib/4.1.1/arthas:/opt/arthas}"
JAVA_PATTERN="${ARTHAS_JAVA_PATTERN:-app.jar}"
echo "pod: $POD"
# Find as.sh and the Java pid, then run Arthas in batch mode (-c).
# $1 = colon search paths (converted to spaces inside the Pod),
# $2 = pgrep pattern, $3 = the Arthas command. The MCP server uses
# the same pattern, with the same two values as server-side config.
kubectl -n "$NS" exec "$POD" -- bash -lc '
  SP="${1//:/ }"
  AS=$(find $SP -name as.sh -type f 2>/dev/null | head -n 1)
  PID=$(pgrep -f "$2" | head -n 1)
  echo "as.sh: $AS pid: $PID" >&2
  "$AS" --select "$PID" -c "$3"
' _ "$SEARCH_PATHS" "$JAVA_PATTERN" "$*"
