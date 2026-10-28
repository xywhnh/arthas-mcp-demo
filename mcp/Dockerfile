# MCP HTTP server image (independent service, NOT the demo app image).
#
# The demo app image is Java + Arthas and contains no Python, no MCP
# code, and no kubectl, so it cannot run this server. This image is
# Python + kubectl + the two MCP server files, and nothing else.
#
# Inside a Pod, kubectl needs no kubeconfig file: it discovers the
# cluster API server and the Pod's own ServiceAccount token
# (mounted automatically at /var/run/secrets/kubernetes.io/serviceaccount)
# and uses them. That is why the MCP Pod in gzdt can exec into the
# arthas-demo-service Pods in the same namespace with only a namespaced
# Role (pods get/list, pods/exec create), and why no host process,
# host file, or host kubectl is involved at all.
#
# kubectl is pinned to 1.31.0 (a mid-range client that works against
# a wide range of server versions for get/list/exec). TARGETARCH makes
# the amd64/arm64 Buildx builds fetch the matching binary.
FROM python:3.12-slim

ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/v1.31.0/bin/linux/${TARGETARCH}/kubectl" \
 && chmod +x /usr/local/bin/kubectl \
 && kubectl version --client

WORKDIR /app
COPY mcp/ /app/mcp/
EXPOSE 18080

# Host/port are arguments (not baked in) so the same image can run on
# any port; the gzdt Deployment passes --host 0.0.0.0 --port 18080.
# ARTHAS_* / MCP_AUTH_TOKEN come from the Deployment environment.
CMD ["python3", "/app/mcp/mcp_http_server.py", "--host", "0.0.0.0", "--port", "18080"]
