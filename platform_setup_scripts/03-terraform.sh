#!/usr/bin/env bash
# Phase 03 — Terraform init + plan + apply (with safety gate)
#
# DEFAULT BEHAVIOR (safe):
#   1. Clean .terraform/ cache (backend may have changed)
#   2. terraform init against the configured GCS backend
#   3. terraform plan -out=tfplan
#   4. PROMPT user: "Apply this plan? [y/N]"
#   5. On 'y' → apply. On anything else → exit, tell user how to apply manually.
#   6. After apply, switch kubectl context to new cluster
#
# OPT-IN OVERRIDES:
#   TF_AUTO_APPROVE=true   skip the prompt (for unattended runs — use carefully)
#   PLAN_ONLY=true         exit after plan, never apply (manual apply later)
#
# FAILURE MODE: no auto-retries. If apply errors, you investigate.
#   The known WI Pool race is now prevented by depends_on in eso.tf — if it
#   somehow still fires, re-run this script; the cluster will already exist
#   so the WI pool will be ready by the next plan/apply.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

log_step "Phase 03 — Terraform"

require_env PROJECT_ID REGION ZONE CLUSTER_NAME TF_STATE_BUCKET

TF_DIR="$REPO_ROOT/gke_terraform"
[ -d "$TF_DIR" ] || { log_err "gke_terraform directory not found at $TF_DIR"; exit 1; }

cd "$TF_DIR"

# --- 1. Clean stale backend cache ---
if [ -d ".terraform" ]; then
  log_info "Removing .terraform/ cache (previous backend may differ from $TF_STATE_BUCKET)"
  run rm -rf .terraform/
fi

# --- 2. Init ---
log_info "terraform init"
run terraform init \
  -backend-config="bucket=$TF_STATE_BUCKET" \
  -backend-config="prefix=gke/terraform.tfstate"

# --- 3. Plan ---
log_info "terraform plan (output to tfplan)"
run terraform plan \
  -var="project_id=$PROJECT_ID" \
  -var="region=$REGION" \
  -var="zone=$ZONE" \
  -var="cluster_name=$CLUSTER_NAME" \
  -var="github_repo=${GITHUB_REPO:-amoghjay/k8s-chaos-promotion}" \
  -out=tfplan

# --- 4. Safety gate ---
if [ "$DRY_RUN" = "true" ]; then
  log_info "(dry-run: not applying)"
  cd "$REPO_ROOT"
  log_ok "Phase 03 complete (dry-run)"
  exit 0
fi

if [ "${PLAN_ONLY:-false}" = "true" ]; then
  log_info "PLAN_ONLY=true — plan saved. Apply manually with:"
  echo ""
  echo "    cd gke_terraform && terraform apply tfplan"
  echo ""
  exit 0
fi

if [ "${TF_AUTO_APPROVE:-false}" = "true" ]; then
  log_warn "TF_AUTO_APPROVE=true — applying without prompt"
  CONFIRM="y"
else
  echo ""
  log_warn "Review the plan above ↑↑↑"
  printf '%s[CONFIRM]%s  Apply this Terraform plan? [y/N] ' "$C_YELLOW" "$C_RESET" >&2
  read -r CONFIRM
  echo ""
fi

if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  log_info "Aborted by user. Plan is saved at gke_terraform/tfplan — apply manually anytime with:"
  echo ""
  echo "    cd gke_terraform && terraform apply tfplan"
  echo ""
  exit 0
fi

# --- 5. Apply (no -auto-approve; we just gave consent via prompt) ---
log_info "terraform apply tfplan"
terraform apply tfplan

# --- 6. Switch kubectl context ---
log_info "Switching kubectl context to new cluster"
run gcloud container clusters get-credentials "$CLUSTER_NAME" --zone="$ZONE" --project="$PROJECT_ID"

CTX=$(kubectl config current-context)
log_ok "kubectl context: $CTX"

cd "$REPO_ROOT"
log_ok "Phase 03 complete"
