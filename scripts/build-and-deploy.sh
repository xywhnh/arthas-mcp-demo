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
# Same namespace race as CI: create the namespace first and wait for it
# before applying Deployment/Service in the same cluster.
kubectl apply -f k8s/namespace.yaml
for i in $(seq 1 30); do
  kubectl get namespace demo-arthas >/dev/null 2>&1 && break
  sleep 2
done
sleep 5
kubectl apply -f k8s/deployment.yaml -f k8s/service.yaml
kubectl -n demo-arthas rollout status deployment/demo-service --timeout=180s
kubectl -n demo-arthas get pods -o wide
echo "Done. Next: scripts/smoke.sh"
