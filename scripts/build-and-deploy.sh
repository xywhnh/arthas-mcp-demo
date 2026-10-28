#!/usr/bin/env bash
# Build the Java app, build the image, load it into kind, deploy to k8s.
# Run from the repo root (arthas-mcp-demo/).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1. Build jar =="
mvn -q -pl apps/demo-service -am package -DskipTests

echo "== 2. Build image =="
docker build -t demo-service:0.1.0 apps/demo-service

echo "== 3. Load into kind =="
kind load docker-image demo-service:0.1.0 --name "${CLUSTER:-arthas-demo}"

echo "== 4. Deploy =="
kubectl apply -f k8s/
kubectl -n demo-arthas rollout status deployment/demo-service --timeout=180s
kubectl -n demo-arthas get pods -o wide
echo "Done. Next: scripts/smoke.sh"
