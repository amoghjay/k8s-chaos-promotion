# Runner image for the chaos-gate orchestrator: bash + kubectl + python3.
# python:slim already ships bash + python3 (score_experiment.py is stdlib-only,
# no pip), so we only add the kubectl binary. ~150MB.
#
# Why not google/cloud-sdk: it does NOT bundle kubectl (stable/slim exclude it,
# latest doesn't list it) — it'd need a flaky runtime `gcloud components install`
# on every gate run. Baking kubectl here makes the gate's tooling deterministic.
#
# Build + push (GAR is public → no pull secret needed):
#   docker build --platform linux/amd64 -t \
#     us-central1-docker.pkg.dev/ajprojectplatform/k8s-chaos-demo/gate-runner:v1 \
#     docker/gate-runner
#   docker push us-central1-docker.pkg.dev/ajprojectplatform/k8s-chaos-demo/gate-runner:v1
FROM python:3.12-slim

# Pin kubectl one minor below/at the cluster's server version (GKE skew policy
# tolerates ±1 minor). Cluster is on v1.35.x → 1.34 is the in-skew floor.
# Bump when the cluster upgrades.
ARG KUBECTL_VERSION=v1.34.9

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/$(dpkg --print-architecture)/kubectl"; \
    chmod +x /usr/local/bin/kubectl; \
    kubectl version --client; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

# Scripts arrive at runtime via the chaos-gate-scripts ConfigMap mount (/scripts);
# nothing app-specific is baked in — this image is a generic bash+kubectl+python runner.
