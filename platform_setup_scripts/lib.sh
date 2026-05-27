#!/usr/bin/env bash
# Shared library for platform_setup_scripts.
# Sourced by every phase script + the orchestrator.
#
# Functions:
#   log_info/warn/err/ok   — colored stderr logging
#   require_env            — assert env var is set
#   gcloud_active          — verify gcloud auth + project context
#   secret_exists          — true/false probe for a GCP secret (idempotency helper)
#   k8s_apply              — wrapper that respects DRY_RUN
#   run                    — wrapper that respects DRY_RUN
#   wait_for               — wait until a kubectl JSONPath condition is met

set -euo pipefail

# Colors only when stderr is a TTY
if [ -t 2 ]; then
  C_RESET=$'\033[0m' C_GREEN=$'\033[32m' C_YELLOW=$'\033[33m' C_RED=$'\033[31m' C_BLUE=$'\033[34m' C_DIM=$'\033[2m'
else
  C_RESET= C_GREEN= C_YELLOW= C_RED= C_BLUE= C_DIM=
fi

DRY_RUN="${DRY_RUN:-false}"

log_info()  { printf '%s[INFO]%s  %s\n' "$C_BLUE"   "$C_RESET" "$*" >&2; }
log_ok()    { printf '%s[ OK ]%s  %s\n' "$C_GREEN"  "$C_RESET" "$*" >&2; }
log_warn()  { printf '%s[WARN]%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
log_err()   { printf '%s[ERR ]%s  %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }
log_step()  { printf '\n%s──▶ %s%s\n'   "$C_BLUE"   "$*"        "$C_RESET" >&2; }

run() {
  if [ "$DRY_RUN" = "true" ]; then
    printf '%s$ %s%s\n' "$C_DIM" "$*" "$C_RESET" >&2
  else
    "$@"
  fi
}

require_env() {
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then
      log_err "Required env var \$$v is not set"
      exit 1
    fi
  done
}

gcloud_active() {
  local account project
  account=$(gcloud config get-value account 2>/dev/null)
  project=$(gcloud config get-value project 2>/dev/null)
  log_info "gcloud account: ${account:-<unset>}"
  log_info "gcloud project: ${project:-<unset>}"
  if [ "$project" != "$PROJECT_ID" ]; then
    log_warn "gcloud project ($project) != PROJECT_ID ($PROJECT_ID). Will pass --project=$PROJECT_ID explicitly."
  fi
}

secret_exists() {
  # secret_exists <secret-name> <project-id> [account-flag]
  gcloud secrets describe "$1" --project="$2" ${3:+--account="$3"} >/dev/null 2>&1
}

k8s_apply() {
  # Apply a manifest. Respects DRY_RUN.
  if [ "$DRY_RUN" = "true" ]; then
    printf '%s$ kubectl apply -f %s%s\n' "$C_DIM" "$*" "$C_RESET" >&2
  else
    kubectl apply -f "$@"
  fi
}

wait_for() {
  # wait_for "<description>" "<command that exits 0 when ready>" [timeout-seconds]
  local desc="$1" check="$2" timeout="${3:-180}"
  local elapsed=0
  log_info "Waiting for: $desc (timeout ${timeout}s)"
  while ! eval "$check" >/dev/null 2>&1; do
    if [ "$elapsed" -ge "$timeout" ]; then
      log_err "Timed out after ${timeout}s waiting for: $desc"
      return 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  log_ok "Ready: $desc (took ${elapsed}s)"
}

# Detect repo root (assumes lib.sh lives in <repo>/platform_setup_scripts/)
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/platform_setup_scripts"
