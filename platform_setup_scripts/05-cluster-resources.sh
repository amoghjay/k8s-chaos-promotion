#!/usr/bin/env bash
# Phase 05 — apply ClusterSecretStore + ArgoCD repo ExternalSecret (idempotent).
# Grafana ExternalSecret is deferred to Phase 06: its target ns (monitoring)
# doesn't exist until root-app syncs.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

log_step "Phase 05 — cluster resources (ESO config + ArgoCD repo secret)"

require_env PROJECT_ID

CSS_FILE="$REPO_ROOT/kubernetes/bootstrap/secrets/cluster-secret-store.yaml"
ARGOCD_ES_FILE="$REPO_ROOT/kubernetes/bootstrap/secrets/external-secrets-argocd.yaml"

for f in "$CSS_FILE" "$ARGOCD_ES_FILE"; do
  [ -f "$f" ] || { log_err "Missing file: $f"; exit 1; }
done

# --- 1. ClusterSecretStore ---
log_info "Applying ClusterSecretStore"
k8s_apply "$CSS_FILE"

if [ "$DRY_RUN" != "true" ]; then
  wait_for "ClusterSecretStore gcp-secret-manager Ready" \
    "kubectl get clustersecretstore gcp-secret-manager -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}' 2>/dev/null | grep -q True" \
    60
fi

# --- 2. ArgoCD repo ExternalSecret ---
log_info "Applying ArgoCD repo ExternalSecret (GitHub PAT)"
k8s_apply "$ARGOCD_ES_FILE"

if [ "$DRY_RUN" != "true" ]; then
  wait_for "ArgoCD repo K8s Secret created by ESO" \
    "kubectl get secret repo-k8s-chaos-promotion -n argocd >/dev/null 2>&1" \
    60

  # Verify label so ArgoCD discovers it as a repo credential
  LABEL=$(kubectl get secret repo-k8s-chaos-promotion -n argocd -o jsonpath='{.metadata.labels.argocd\.argoproj\.io/secret-type}' 2>/dev/null || echo "")
  if [ "$LABEL" = "repository" ]; then
    log_ok "ArgoCD repo secret labeled correctly (secret-type: repository)"
  else
    log_warn "Repo secret missing 'argocd.argoproj.io/secret-type: repository' label — got '$LABEL'"
  fi
fi

log_ok "Phase 05 complete"
log_info "Note: Grafana ExternalSecret (external-secrets-monitoring.yaml) intentionally deferred."
log_info "      It is applied in Phase 06 after root-app creates the monitoring namespace."
