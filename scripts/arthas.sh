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
# This script mirrors mcp/arthas_mcp_server.py and does NOT use as.sh:
# as.sh exits 1 when the system telnet binary is missing (real service
# container has none), even though batch commands never use that
# binary. Instead it runs arthas-client.jar (Java) directly, and if
# the client reports Connection refused (agent not attached yet),
# attaches once with arthas-core.jar (Java) and retries.
#   ARTHAS_SEARCH_PATHS  default "/usr/app/tools/arthas:/usr/app/.arthas/lib/4.1.1/arthas:/opt/arthas"
#   ARTHAS_JAVA_PATTERN  default "app.jar"
set -euo pipefail
NS="${NS:-demo-arthas}"
APP_LABEL="${APP_LABEL:-demo-service}"
POD="${POD:-$(kubectl -n "$NS" get pods -l app="$APP_LABEL" -o jsonpath='{.items[0].metadata.name}')}"
SEARCH_PATHS="${ARTHAS_SEARCH_PATHS:-/usr/app/tools/arthas:/usr/app/.arthas/lib/4.1.1/arthas:/opt/arthas}"
JAVA_PATTERN="${ARTHAS_JAVA_PATTERN:-app.jar}"
echo "pod: $POD"
# $1 = colon search paths (converted to spaces inside the Pod),
# $2 = pgrep pattern, $3 = the Arthas command.
kubectl -n "$NS" exec "$POD" -- bash -lc '
  SP="${1//:/ }"
  CJ=$(find $SP -name arthas-client.jar -type f 2>/dev/null | head -n 1)
  AD=$(dirname "$CJ" 2>/dev/null)
  PID=$(pgrep -f "$2" | head -n 1)
  echo "client_jar: $CJ arthas_dir: $AD pid: $PID" >&2
  if ! java -jar "$AD/arthas-client.jar" 127.0.0.1 3658 -c "$3"; then
    echo "client failed, attaching agent with Java only, then retrying..." >&2
    java -jar "$AD/arthas-core.jar" -pid "$PID" -core "$AD/arthas-core.jar" -agent "$AD/arthas-agent.jar"
    java -jar "$AD/arthas-client.jar" 127.0.0.1 3658 -c "$3"
  fi
' _ "$SEARCH_PATHS" "$JAVA_PATTERN" "$*"
