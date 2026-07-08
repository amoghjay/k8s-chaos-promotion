# Runner image for the chaos-gate orchestrator: bash + kubectl + python3.
# python:slim already ships bash + python3 (the scoring script is stdlib-only),
# so only kubectl is added. google/cloud-sdk was rejected because it doesn't
# bundle kubectl and would need a flaky runtime install on every gate run.
FROM python:3.12-slim

# Pin kubectl within ±1 minor of the cluster's server version (GKE skew
# policy). Cluster is on v1.35.x; bump when the cluster upgrades.
ARG KUBECTL_VERSION=v1.34.9

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/$(dpkg --print-architecture)/kubectl"; \
    chmod +x /usr/local/bin/kubectl; \
    kubectl version --client; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

# Scripts arrive at runtime via the chaos-gate-scripts ConfigMap mount
# (/scripts); nothing app-specific is baked in.
