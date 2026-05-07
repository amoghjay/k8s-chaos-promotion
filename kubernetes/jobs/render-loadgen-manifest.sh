#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

LOADGEN_SCRIPT="$PROJECT_ROOT/scripts/loadgen.js"
JOB_MANIFEST="$SCRIPT_DIR/loadgen-job.yaml"
LOADGEN_IMAGE=${LOADGEN_IMAGE:-}

if [ -z "$LOADGEN_IMAGE" ]; then
  echo "LOADGEN_IMAGE is required." >&2
  echo "Example:" >&2
  echo "  LOADGEN_IMAGE=us-central1-docker.pkg.dev/amoghdevops/k8s-chaos-demo/k6-ethereum:sha-<short> \\" >&2
  echo "    ./kubernetes/jobs/render-loadgen-manifest.sh | kubectl apply -f -" >&2
  exit 1
fi

kubectl create configmap k6-loadgen-script \
  --namespace url-shortener-staging \
  --from-file=loadgen.js="$LOADGEN_SCRIPT" \
  --dry-run=client \
  -o yaml

printf '%s\n' '---'
sed "s|__LOADGEN_IMAGE__|$LOADGEN_IMAGE|g" "$JOB_MANIFEST"
