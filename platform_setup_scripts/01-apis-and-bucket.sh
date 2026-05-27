#!/usr/bin/env bash
# Phase 01 — enable required GCP APIs + create Terraform state GCS bucket
# Idempotent: re-running is safe; describes before creating.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

log_step "Phase 01 — APIs + TF state bucket"

require_env PROJECT_ID REGION TF_STATE_BUCKET

# --- APIs to enable ---
APIS=(
  compute.googleapis.com
  container.googleapis.com
  artifactregistry.googleapis.com
  secretmanager.googleapis.com
  iamcredentials.googleapis.com
  sts.googleapis.com
  iam.googleapis.com
)

log_info "Enabling APIs in $PROJECT_ID (skips already-enabled in one call)"
run gcloud services enable "${APIS[@]}" --project="$PROJECT_ID"
log_ok "APIs enabled"

# --- TF state bucket ---
if gcloud storage buckets describe "gs://$TF_STATE_BUCKET" --project="$PROJECT_ID" >/dev/null 2>&1; then
  log_ok "TF state bucket gs://$TF_STATE_BUCKET already exists"
else
  log_info "Creating TF state bucket gs://$TF_STATE_BUCKET"
  run gcloud storage buckets create "gs://$TF_STATE_BUCKET" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --uniform-bucket-level-access
fi

# --- Enable versioning (idempotent — no-op if already on) ---
versioning=$(gcloud storage buckets describe "gs://$TF_STATE_BUCKET" --project="$PROJECT_ID" --format='value(versioning.enabled)' 2>/dev/null || echo "")
if [ "$versioning" != "True" ]; then
  log_info "Enabling versioning on bucket"
  run gcloud storage buckets update "gs://$TF_STATE_BUCKET" --versioning
else
  log_ok "Versioning already enabled"
fi

log_ok "Phase 01 complete"
