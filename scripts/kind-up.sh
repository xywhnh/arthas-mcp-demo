#!/usr/bin/env bash
# Create a local kind cluster for the demo. Requires: docker, kind, kubectl.
set -euo pipefail
CLUSTER="${CLUSTER:-arthas-demo}"
if kind get clusters | grep -qx "$CLUSTER"; then
  echo "kind cluster '$CLUSTER' already exists"
else
  kind create cluster --name "$CLUSTER"
fi
kubectl cluster-info --context "kind-$CLUSTER"
