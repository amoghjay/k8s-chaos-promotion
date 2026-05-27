#!/usr/bin/env bash
# bootstrap.sh — orchestrator for platform_setup_scripts
#
# Usage:
#   ./bootstrap.sh                              # run all phases (00 → 07) in order
#   ./bootstrap.sh --phase 3                    # run only phase 03
#   ./bootstrap.sh --from 3                     # run phase 03 onwards
#   ./bootstrap.sh --to 3                       # run phases 00 → 03 only
#   ./bootstrap.sh --source-project amoghdevops \
#                  --source-account amoghjay.us@gmail.com   # migration mode
#   ./bootstrap.sh --dry-run                    # print commands without executing
#   ./bootstrap.sh --help
#
# Reads config from ./config.env (copy from config.env.example).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
bootstrap.sh — k8s-chaos-promotion platform bootstrap

Usage:
  $0 [--phase N | --from N | --to N] [--source-project ID --source-account EMAIL] [--dry-run]

Flags:
  --phase N           Run only phase N (00..07)
  --from N            Start from phase N (inclusive)
  --to N              Stop at phase N (inclusive)
  --source-project    GCP project ID to migrate secrets FROM
  --source-account    gcloud account with access to source project
  --dry-run           Print commands without executing
  --help              Show this help

Phases:
  00  preflight              — tool + auth checks
  01  apis-and-bucket        — enable GCP APIs + create TF state bucket
  02  secrets                — create OR migrate secrets
  03  terraform              — provision GKE + GAR + WIF + SAs (with safety prompt)
  04  platform               — helm install cert-manager, ArgoCD, Argo Rollouts, ESO, Kargo
  05  cluster-resources      — ClusterSecretStore + ArgoCD repo ExternalSecret
  06  gitops-and-kargo       — root-app + Kargo CRs + ApplicationSet (correct order)
  07  verify                 — health checks (read-only)

Examples:
  Fresh setup:
    cp config.env.example config.env && \$EDITOR config.env
    ./bootstrap.sh

  Migration from old project:
    ./bootstrap.sh --source-project amoghdevops --source-account amoghjay.us@gmail.com

  Resume from a specific phase:
    ./bootstrap.sh --from 4

  Plan-only (Terraform plan, no apply):
    PLAN_ONLY=true ./bootstrap.sh --phase 3
EOF
}

# --- Parse args ---
PHASE_SINGLE=""
PHASE_FROM=""
PHASE_TO=""
export SOURCE_PROJECT="${SOURCE_PROJECT:-}"
export SOURCE_ACCOUNT="${SOURCE_ACCOUNT:-}"
export DRY_RUN="${DRY_RUN:-false}"

while [ $# -gt 0 ]; do
  case "$1" in
    --phase)          PHASE_SINGLE="$2"; shift 2 ;;
    --from)           PHASE_FROM="$2"; shift 2 ;;
    --to)             PHASE_TO="$2"; shift 2 ;;
    --source-project) SOURCE_PROJECT="$2"; shift 2 ;;
    --source-account) SOURCE_ACCOUNT="$2"; shift 2 ;;
    --dry-run)        DRY_RUN=true; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                echo "Unknown flag: $1" >&2; usage; exit 2 ;;
  esac
done

# --- Load config ---
CONFIG_FILE="$SCRIPT_DIR/config.env"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "[ERR ] $CONFIG_FILE not found. Run: cp config.env.example config.env && \$EDITOR config.env" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG_FILE"

# Export everything for child scripts
export PROJECT_ID PROJECT_NUMBER REGION ZONE CLUSTER_NAME GAR_REPO GITHUB_REPO TF_STATE_BUCKET
export CERT_MANAGER_VERSION ESO_VERSION KARGO_CHART
export SECRETS

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

# --- Plan phases ---
ALL_PHASES=(00 01 02 03 04 05 06 07)
PHASE_FILES=(
  "$SCRIPT_DIR/00-preflight.sh"
  "$SCRIPT_DIR/01-apis-and-bucket.sh"
  "$SCRIPT_DIR/02-secrets.sh"
  "$SCRIPT_DIR/03-terraform.sh"
  "$SCRIPT_DIR/04-platform.sh"
  "$SCRIPT_DIR/05-cluster-resources.sh"
  "$SCRIPT_DIR/06-gitops-and-kargo.sh"
  "$SCRIPT_DIR/07-verify.sh"
)

normalize_phase() {
  # Accepts "3" or "03" → "03"
  printf '%02d' "$1"
}

select_phases() {
  local start=0 end=$((${#ALL_PHASES[@]} - 1))
  if [ -n "$PHASE_SINGLE" ]; then
    local target; target=$(normalize_phase "$PHASE_SINGLE")
    for i in "${!ALL_PHASES[@]}"; do
      [ "${ALL_PHASES[$i]}" = "$target" ] && { start=$i; end=$i; }
    done
  else
    if [ -n "$PHASE_FROM" ]; then
      local target; target=$(normalize_phase "$PHASE_FROM")
      for i in "${!ALL_PHASES[@]}"; do
        [ "${ALL_PHASES[$i]}" = "$target" ] && start=$i
      done
    fi
    if [ -n "$PHASE_TO" ]; then
      local target; target=$(normalize_phase "$PHASE_TO")
      for i in "${!ALL_PHASES[@]}"; do
        [ "${ALL_PHASES[$i]}" = "$target" ] && end=$i
      done
    fi
  fi
  echo "$start $end"
}

read -r START END <<< "$(select_phases)"

# --- Summary ---
log_step "platform_setup_scripts — bootstrap"
log_info "  project: $PROJECT_ID"
log_info "  region/zone: $REGION / $ZONE"
log_info "  cluster: $CLUSTER_NAME"
[ -n "$SOURCE_PROJECT" ] && log_info "  migration mode: source = $SOURCE_PROJECT (account: ${SOURCE_ACCOUNT:-<current>})"
[ "$DRY_RUN" = "true" ] && log_warn "  DRY-RUN — no changes will be applied"
log_info "  phases to run: ${ALL_PHASES[$START]} → ${ALL_PHASES[$END]}"
echo ""

# --- Run phases ---
for i in $(seq "$START" "$END"); do
  PHASE="${ALL_PHASES[$i]}"
  FILE="${PHASE_FILES[$i]}"
  if [ ! -x "$FILE" ]; then
    chmod +x "$FILE" 2>/dev/null || true
  fi
  "$FILE"
done

log_step "All requested phases complete"
