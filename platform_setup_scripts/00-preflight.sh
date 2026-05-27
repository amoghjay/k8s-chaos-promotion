#!/usr/bin/env bash
# Phase 00 — preflight checks
# Verifies the operator's local environment has everything needed before we touch any cloud or cluster state.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

log_step "Phase 00 — preflight"

require_env PROJECT_ID

# --- 1. CLI tools present ---
declare -A REQUIRED_TOOLS=(
  [gcloud]="brew install --cask google-cloud-sdk"
  [kubectl]="brew install kubectl"
  [helm]="brew install helm"
  [terraform]="brew install terraform"
  [htpasswd]="brew install httpd"
  [openssl]="(should be pre-installed)"
  [shasum]="(should be pre-installed)"
  [jq]="brew install jq"
  [gh]="brew install gh"
)

missing=0
for tool in "${!REQUIRED_TOOLS[@]}"; do
  if command -v "$tool" >/dev/null 2>&1; then
    log_ok "$tool present"
  else
    log_err "$tool MISSING — install: ${REQUIRED_TOOLS[$tool]}"
    missing=1
  fi
done
[ "$missing" -eq 0 ] || { log_err "Install missing tools and retry"; exit 1; }

# --- 2. gcloud auth ---
gcloud_active

# Verify ADC is set up (Terraform reads from ADC, not gcloud auth)
if gcloud auth application-default print-access-token >/dev/null 2>&1; then
  log_ok "Application Default Credentials configured"
else
  log_warn "ADC not configured. Terraform will fail to authenticate."
  log_warn "Run: gcloud auth application-default login"
  exit 1
fi

# --- 3. Project exists + we can access it ---
if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  PROJECT_NUMBER_DETECTED=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
  log_ok "Project $PROJECT_ID accessible (number: $PROJECT_NUMBER_DETECTED)"
  if [ -n "${PROJECT_NUMBER:-}" ] && [ "$PROJECT_NUMBER" != "$PROJECT_NUMBER_DETECTED" ]; then
    log_warn "Configured PROJECT_NUMBER ($PROJECT_NUMBER) != actual ($PROJECT_NUMBER_DETECTED). Will use actual."
  fi
  export PROJECT_NUMBER="$PROJECT_NUMBER_DETECTED"
else
  log_err "Cannot describe project $PROJECT_ID — check it exists and you have access"
  exit 1
fi

# --- 4. Optional: source project access if migrating ---
if [ -n "${SOURCE_PROJECT:-}" ]; then
  log_info "Migration mode — verifying access to source project: $SOURCE_PROJECT"
  if gcloud projects describe "$SOURCE_PROJECT" ${SOURCE_ACCOUNT:+--account="$SOURCE_ACCOUNT"} >/dev/null 2>&1; then
    log_ok "Source project $SOURCE_PROJECT accessible"
  else
    log_err "Cannot describe source project $SOURCE_PROJECT with account ${SOURCE_ACCOUNT:-<current>}"
    log_err "Run: gcloud auth login  (to add credentials for the source project owner)"
    exit 1
  fi
fi

log_ok "Phase 00 complete"
