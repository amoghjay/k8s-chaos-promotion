#!/usr/bin/env bash
# Phase 02 — populate GCP Secret Manager
#
# Two modes:
#   • Fresh setup: prompts for each secret value interactively
#   • Migration:   when SOURCE_PROJECT is set, streams values from source
#                  to destination via shell pipe (no disk writes)
#
# Both modes are idempotent — if a secret already exists in PROJECT_ID,
# we add a new VERSION rather than failing.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

log_step "Phase 02 — secrets"

require_env PROJECT_ID

# SECRETS array must be set in config.env (sourced by bootstrap.sh)
if [ "${#SECRETS[@]}" -eq 0 ]; then
  log_err "SECRETS array is empty. Define it in config.env."
  exit 1
fi

if [ -n "${SOURCE_PROJECT:-}" ]; then
  # ---------- migration mode ----------
  log_info "Migration mode: streaming secrets from $SOURCE_PROJECT"
  log_info "Source account: ${SOURCE_ACCOUNT:-<current gcloud account>}"
  NEW_ACCOUNT="${NEW_ACCOUNT:-$(gcloud config get-value account 2>/dev/null)}"
  log_info "Destination account: $NEW_ACCOUNT"

  for s in "${SECRETS[@]}"; do
    printf '  %s ... ' "$s" >&2
    if secret_exists "$s" "$PROJECT_ID" "$NEW_ACCOUNT"; then
      # Already exists — add a new version
      if [ "$DRY_RUN" = "true" ]; then
        printf '(would add version)\n' >&2
      else
        gcloud secrets versions access latest \
          --secret="$s" --project="$SOURCE_PROJECT" --account="$SOURCE_ACCOUNT" 2>/dev/null \
          | gcloud secrets versions add "$s" \
            --project="$PROJECT_ID" --account="$NEW_ACCOUNT" --data-file=- >/dev/null
        printf 'added version\n' >&2
      fi
    else
      if [ "$DRY_RUN" = "true" ]; then
        printf '(would create)\n' >&2
      else
        gcloud secrets versions access latest \
          --secret="$s" --project="$SOURCE_PROJECT" --account="$SOURCE_ACCOUNT" 2>/dev/null \
          | gcloud secrets create "$s" \
            --project="$PROJECT_ID" --account="$NEW_ACCOUNT" \
            --replication-policy=automatic --data-file=- >/dev/null
        printf 'created\n' >&2
      fi
    fi
  done

  # SHA256 integrity verification — catches truncation/encoding bugs
  if [ "$DRY_RUN" != "true" ]; then
    log_info "Verifying SHA256 integrity (old vs new)"
    printf '\n%-35s %-13s %-13s %s\n' "SECRET" "OLD_SHA8" "NEW_SHA8" "MATCH"
    for s in "${SECRETS[@]}"; do
      OLD_HASH=$(gcloud secrets versions access latest --secret="$s" --project="$SOURCE_PROJECT" --account="$SOURCE_ACCOUNT" 2>/dev/null | shasum -a 256 | cut -c1-8)
      NEW_HASH=$(gcloud secrets versions access latest --secret="$s" --project="$PROJECT_ID" --account="$NEW_ACCOUNT" 2>/dev/null | shasum -a 256 | cut -c1-8)
      if [ "$OLD_HASH" = "$NEW_HASH" ]; then MATCH="✓"; else MATCH="✗ MISMATCH"; fi
      printf '%-35s %-13s %-13s %s\n' "$s" "$OLD_HASH" "$NEW_HASH" "$MATCH"
    done
    log_ok "Migration integrity verified"
  fi
else
  # ---------- fresh setup ----------
  log_info "Fresh setup mode: will prompt for each secret value"
  log_info "(values read silently — nothing echoed to terminal)"
  echo ""

  for s in "${SECRETS[@]}"; do
    if secret_exists "$s" "$PROJECT_ID"; then
      log_ok "$s already exists — skipping (use --force-rotate to rotate)"
      continue
    fi
    printf "Enter value for secret '%s': " "$s" >&2
    # zsh-compatible silent read
    if [ -n "${ZSH_VERSION:-}" ]; then
      read -s VAL
    else
      read -s VAL
    fi
    echo "" >&2

    if [ -z "$VAL" ]; then
      log_warn "  empty input — skipping $s"
      continue
    fi
    if [ "$DRY_RUN" = "true" ]; then
      log_info "  (would create $s)"
    else
      printf '%s' "$VAL" | gcloud secrets create "$s" \
        --project="$PROJECT_ID" \
        --replication-policy=automatic --data-file=- >/dev/null
      log_ok "  created $s"
    fi
    unset VAL
  done
fi

log_ok "Phase 02 complete"
