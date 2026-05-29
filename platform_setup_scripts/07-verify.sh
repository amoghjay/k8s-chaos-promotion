#!/usr/bin/env bash
# Phase 07 — verify the platform is healthy end-to-end
#
# Read-only. Doesn't apply or change anything. Just probes state.
# Returns non-zero if any critical check fails.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

log_step "Phase 07 — verification"

require_env PROJECT_ID CLUSTER_NAME ZONE

fail=0
check() {
  # check "description" "command that exits 0 on success"
  local desc="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    log_ok "$desc"
  else
    log_err "$desc"
    fail=$((fail + 1))
  fi
}

# --- kubectl context ---
EXPECTED_CTX="gke_${PROJECT_ID}_${ZONE}_${CLUSTER_NAME}"
ACTUAL_CTX=$(kubectl config current-context 2>/dev/null || echo "<unset>")
if [ "$ACTUAL_CTX" = "$EXPECTED_CTX" ]; then
  log_ok "kubectl context: $ACTUAL_CTX"
else
  log_warn "kubectl context: $ACTUAL_CTX (expected: $EXPECTED_CTX)"
fi

# --- Platform pods Running ---
log_info "Platform pods:"
for ns in cert-manager argocd argo-rollouts external-secrets kargo; do
  pending=$(kubectl get pods -n "$ns" --field-selector=status.phase!=Running 2>/dev/null | grep -vE "^NAME|Completed" | wc -l | tr -d ' ')
  if [ "$pending" = "0" ]; then
    log_ok "  $ns: all pods Running"
  else
    log_err "  $ns: $pending pods not Running"
    kubectl get pods -n "$ns" --field-selector=status.phase!=Running 2>/dev/null | grep -vE "^NAME|Completed" >&2
    fail=$((fail + 1))
  fi
done

# --- ESO synced ---
log_info "ExternalSecrets:"
unsynced=$(kubectl get externalsecret -A 2>/dev/null | awk 'NR>1 && $7 != "SecretSynced" {print}' | wc -l | tr -d ' ')
if [ "$unsynced" = "0" ]; then
  log_ok "  All ExternalSecrets SecretSynced=True"
else
  log_err "  $unsynced ExternalSecrets not SecretSynced"
  kubectl get externalsecret -A 2>/dev/null | awk 'NR==1 || $7 != "SecretSynced"' >&2
  fail=$((fail + 1))
fi

# --- ArgoCD apps ---
log_info "ArgoCD apps:"
unsynced_apps=$(kubectl get app -n argocd 2>/dev/null | awk 'NR>1 && ($2 != "Synced" || $3 !~ /Healthy/) {print}' | wc -l | tr -d ' ')
if [ "$unsynced_apps" = "0" ]; then
  log_ok "  All ArgoCD apps Synced + Healthy"
else
  log_warn "  $unsynced_apps ArgoCD apps not fully Synced/Healthy (chaos-jobs Degraded after first install is expected — see LEARNINGS)"
  kubectl get app -n argocd 2>/dev/null >&2
fi

# --- Kargo state ---
log_info "Kargo:"
check "  Warehouse exists" "kubectl get warehouse url-shortener -n url-shortener"
check "  ProjectConfig Ready" "kubectl get projectconfig url-shortener -n url-shortener -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}' | grep -q True"
check "  Stage dev exists" "kubectl get stage dev -n url-shortener"
check "  Stage staging exists" "kubectl get stage staging -n url-shortener"
check "  Stage prod exists" "kubectl get stage prod -n url-shortener"

# --- GAR images present ---
log_info "GAR images in new project:"
for img in url-shortener radius-signer; do
  count=$(gcloud artifacts docker images list \
    "us-central1-docker.pkg.dev/$PROJECT_ID/k8s-chaos-demo/$img" \
    --project="$PROJECT_ID" --include-tags --limit=1 --format='value(IMAGE)' 2>/dev/null | wc -l | tr -d ' ')
  if [ "$count" -ge 1 ]; then
    log_ok "  $img: at least one tag present"
  else
    log_warn "  $img: no images yet — CI build may not have run (push to main with files under filtered paths to trigger)"
  fi
done

echo ""
if [ "$fail" -eq 0 ]; then
  log_ok "Verification PASSED — platform is healthy"
  exit 0
else
  log_err "Verification FAILED — $fail critical issue(s) above"
  exit 1
fi
