#!/usr/bin/env bash
# Smoke test via Service port-forward. Requires the deployment to be Ready.
set -euo pipefail
kubectl -n demo-arthas port-forward svc/demo-service 8080:8080 >/tmp/demo-port-forward.log 2>&1 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null || true' EXIT
sleep 3
echo "--- health ---"
curl -s http://localhost:8080/actuator/health; echo
echo "--- normal order (expect ~20-50ms, mode=normal) ---"
time curl -s http://localhost:8080/api/orders/1; echo
echo "--- fault status ---"
curl -s http://localhost:8080/demo/faults; echo
