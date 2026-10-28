#!/usr/bin/env bash
# Direct Arthas check WITHOUT the MCP server. Run this first: if this fails,
# fix Arthas attach before blaming the MCP layer.
#
# Usage:
#   scripts/arthas.sh dashboard
#   scripts/arthas.sh jvm
#   scripts/arthas.sh 'trace com.example.demo.order.OrderService getOrder -n 3'
# For trace/monitor, generate traffic in another terminal while it runs.
set -euo pipefail
NS="${NS:-demo-arthas}"
POD="${POD:-$(kubectl -n "$NS" get pods -l app=demo-service -o jsonpath='{.items[0].metadata.name}')}"
echo "pod: $POD"
# Find as.sh (zip layout may nest it one level deep) and the Java pid, then
# run Arthas in batch mode (-c). If your Arthas version prefers a different
# batch flag, adjust this one line -- the MCP server uses the same pattern.
kubectl -n "$NS" exec "$POD" -- bash -lc '
  AS=$(find /opt/arthas -name as.sh -type f | head -n 1)
  PID=$(pgrep -f "app.jar" | head -n 1)
  echo "as.sh: $AS pid: $PID" >&2
  "$AS" --select "$PID" -c '"$*"'
' -- "$@"
