#!/usr/bin/env bash
# Phase 04 — Helm installs of platform tools
#
# Order matters:
#   1. cert-manager (Kargo hard dep — its webhooks need TLS certs)
#   2. ArgoCD
#   3. Argo Rollouts (CRDs for Kargo AnalysisTemplates)
#   4. ESO (with WIF annotation — needs the new project's SA email)
#   5. Kargo (last — needs cert-manager CRDs + argo-rollouts CRDs available)
#
# All use `helm upgrade --install` (idempotent) and `--wait` where applicable.
#
# Kargo admin password: prompts interactively unless KARGO_ADMIN_PASSWORD env var is set.
# Token signing key: auto-generated if KARGO_TOKEN_SIGNING_KEY not set.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

log_step "Phase 04 — platform Helm installs"

require_env PROJECT_ID

# --- 1. Helm repos ---
log_info "Adding helm repos"
run helm repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
run helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
run helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
run helm repo update >/dev/null

# --- 2. cert-manager ---
log_info "Installing cert-manager (Kargo hard dep)"
run helm upgrade --install cert-manager jetstack/cert-manager \
  -n cert-manager --create-namespace \
  --set crds.enabled=true \
  --version "${CERT_MANAGER_VERSION:-v1.16.2}" \
  --wait

# --- 3. ArgoCD ---
log_info "Installing ArgoCD"
run helm upgrade --install argocd argo/argo-cd \
  -n argocd --create-namespace

if [ "$DRY_RUN" != "true" ]; then
  wait_for "argocd-server Ready" \
    "kubectl rollout status -n argocd deploy/argocd-server --timeout=10s" 300
fi

# --- 4. Argo Rollouts (CRDs only — needed for Kargo AnalysisTemplates) ---
log_info "Installing Argo Rollouts (CRDs)"
run helm upgrade --install argo-rollouts argo/argo-rollouts \
  -n argo-rollouts --create-namespace

# --- 5. ESO with WIF annotation ---
log_info "Installing ESO (with WIF annotation for new project)"
ESO_VERSION_ARG=""
[ -n "${ESO_VERSION:-}" ] && ESO_VERSION_ARG="--version $ESO_VERSION"

run helm upgrade --install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace \
  --set "serviceAccount.annotations.iam\.gke\.io/gcp-service-account=external-secrets-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  $ESO_VERSION_ARG \
  --wait

# Verify ESO SA got the annotation
if [ "$DRY_RUN" != "true" ]; then
  ESO_SA_ANNO=$(kubectl get sa external-secrets -n external-secrets -o jsonpath='{.metadata.annotations.iam\.gke\.io/gcp-service-account}' 2>/dev/null)
  if [ "$ESO_SA_ANNO" = "external-secrets-sa@${PROJECT_ID}.iam.gserviceaccount.com" ]; then
    log_ok "ESO SA annotation verified: $ESO_SA_ANNO"
  else
    log_err "ESO SA annotation mismatch — got '$ESO_SA_ANNO'"
    exit 1
  fi
fi

# --- 6. Kargo creds ---
log_info "Preparing Kargo admin credentials"
if [ -z "${KARGO_ADMIN_PASSWORD:-}" ]; then
  log_warn "KARGO_ADMIN_PASSWORD not set — will prompt"
  log_warn "SAVE THIS PASSWORD to your password manager — it cannot be recovered"
  if [ -n "${ZSH_VERSION:-}" ]; then
    read -s "KARGO_ADMIN_PASSWORD?Set Kargo admin password: "
  else
    read -s -p "Set Kargo admin password: " KARGO_ADMIN_PASSWORD
  fi
  echo ""
fi

KARGO_PW_HASH=$(htpasswd -bnBC 10 "" "$KARGO_ADMIN_PASSWORD" | tr -d ':\n')
KARGO_TOKEN_KEY="${KARGO_TOKEN_SIGNING_KEY:-$(openssl rand -base64 29 | tr -d '=+/' | cut -c1-32)}"
unset KARGO_ADMIN_PASSWORD

[ "${#KARGO_PW_HASH}" -ge 50 ] || { log_err "Bcrypt hash looks wrong (length ${#KARGO_PW_HASH})"; exit 1; }
[ "${#KARGO_TOKEN_KEY}" -ge 30 ] || { log_err "Token signing key too short"; exit 1; }

# --- 7. Kargo install ---
log_info "Installing Kargo (OCI chart)"
run helm upgrade --install kargo "${KARGO_CHART:-oci://ghcr.io/akuity/kargo-charts/kargo}" \
  -n kargo --create-namespace \
  --set api.adminAccount.passwordHash="$KARGO_PW_HASH" \
  --set api.adminAccount.tokenSigningKey="$KARGO_TOKEN_KEY" \
  --wait

unset KARGO_PW_HASH KARGO_TOKEN_KEY

# --- 8. Final health check ---
if [ "$DRY_RUN" != "true" ]; then
  log_info "Platform pod status:"
  kubectl get pods -A | grep -E "cert-manager|argocd|argo-rollouts|external-secrets|kargo" | grep -vE "Running|Completed" >&2 && {
    log_warn "Some platform pods are not Running yet — give them ~1 min then re-check"
  } || log_ok "All platform pods Running"
fi

log_ok "Phase 04 complete"
