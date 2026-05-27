# platform_setup_scripts

Repeatable bootstrap automation for the **k8s-chaos-promotion** GKE platform. One command spins up the entire stack from a fresh GCP project — or migrates an existing setup to a new project.

Encodes every gotcha hit during the 2026-05-27 migration (full record in [`docs/migrations/2026-05-27-ajprojectplatform.md`](../docs/migrations/2026-05-27-ajprojectplatform.md)).

---

## Quick start

```bash
# 1. Create config
make config
$EDITOR platform_setup_scripts/config.env   # set PROJECT_ID

# 2. Run
make bootstrap                              # fresh install — interactive
# OR
make migrate SOURCE=amoghdevops ACCOUNT=amoghjay.us@gmail.com   # migration mode
```

---

## What it does (phase by phase)

| # | Script | What it does | Safety |
|---|---|---|---|
| 00 | `00-preflight.sh` | Checks tools (`gcloud`, `kubectl`, `helm`, `terraform`, `htpasswd`, `jq`, `gh`), gcloud auth, ADC, project access | Read-only |
| 01 | `01-apis-and-bucket.sh` | Enables GCP APIs, creates TF state bucket with versioning | Idempotent (`describe \|\| create`) |
| 02 | `02-secrets.sh` | Creates secrets in Secret Manager. **Migration mode**: streams values from `--source-project` via shell pipe (no disk), SHA256-verifies integrity | Idempotent (creates new versions on existing secrets) |
| 03 | `03-terraform.sh` | Terraform `init` + `plan` + `apply` (provisions GKE, GAR, WIF, SAs) | **Prompts for confirmation** before apply. Override: `TF_AUTO_APPROVE=true` |
| 04 | `04-platform.sh` | Helm installs in order: cert-manager → ArgoCD → Argo Rollouts → ESO → Kargo | `helm upgrade --install` (idempotent) |
| 05 | `05-cluster-resources.sh` | Apply `ClusterSecretStore` + ArgoCD repo `ExternalSecret` | `kubectl apply` is desired-state |
| 06 | `06-gitops-and-kargo.sh` | root-app → Grafana ES → Kargo CRs → **ApplicationSet** → Warehouse (correct order encoded) | `kubectl apply` is desired-state |
| 07 | `07-verify.sh` | Health checks: platform pods, ExternalSecrets, ArgoCD apps, Kargo state, GAR images | Read-only |

---

## Flags

```bash
./bootstrap.sh                              # all phases
./bootstrap.sh --phase 3                    # one phase
./bootstrap.sh --from 4                     # resume from phase
./bootstrap.sh --to 3                       # up to phase
./bootstrap.sh --source-project amoghdevops --source-account amoghjay.us@gmail.com
./bootstrap.sh --dry-run                    # print commands, no execution
./bootstrap.sh --help
```

### Env overrides

| Var | Effect |
|---|---|
| `DRY_RUN=true` | Print commands without running |
| `TF_AUTO_APPROVE=true` | Skip Terraform apply confirmation prompt |
| `PLAN_ONLY=true` | Terraform: plan only, never apply |
| `KARGO_ADMIN_PASSWORD` | Pre-set Kargo admin password (skips prompt) |
| `KARGO_TOKEN_SIGNING_KEY` | Pre-set Kargo signing key (else auto-generated) |

---

## Encoded gotchas (the ones that bit us during migration)

These are baked into the scripts so they don't bite again:

1. **Terraform WI Pool race** — `eso.tf` now has `depends_on = [google_container_cluster.gke_cluster]`. Phase 03 also does not auto-retry on apply failures — fail-fast with a clear pointer.
2. **CI workflow `paths:` filters** — Phase 02 reminds you when migrating; the Makefile provides `make trigger-builds` for `workflow_dispatch:` runs.
3. **ApplicationSet must precede Warehouse** — encoded as strict ordering in `06-gitops-and-kargo.sh` with a comment explaining why.
4. **Grafana ExternalSecret manual gap** — Phase 06 applies it AFTER root-app creates monitoring ns, with a `wait_for` on the ns existing.
5. **Kargo admin password lost on `unset`** — Phase 04 logs a `SAVE THIS PASSWORD` warning before generating the hash. Set `KARGO_ADMIN_PASSWORD` env var to bypass the prompt.
6. **Kargo service port 443 not 80** — Makefile `portforward` target uses 443.
7. **zsh `read -p` syntax** — scripts probe `$ZSH_VERSION` and use the correct syntax for each shell.
8. **Secret migration integrity** — Phase 02 SHA256-compares old vs new for every secret in migration mode.
9. **ESO SA annotation verification** — Phase 04 reads back the SA annotation after install to catch escape-character bugs in `--set`.
10. **Idempotency contract** — every step assumes it can be re-run safely. Re-running the orchestrator after a failure picks up where it left off.

---

## Common workflows

### Fresh setup on a brand-new GCP project

```bash
gcloud projects create my-new-project
gcloud config set project my-new-project
gcloud auth application-default login
make config && $EDITOR platform_setup_scripts/config.env
make bootstrap
```

### Migrate from an existing project (credits expired, etc.)

```bash
# Auth to both source + target accounts
gcloud auth login   # for both, in turn
make migrate SOURCE=amoghdevops ACCOUNT=amoghjay.us@gmail.com
```

### Recover from a partial bootstrap

```bash
make phase-04        # rerun just the helm installs
# or
./platform_setup_scripts/bootstrap.sh --from 4
```

### Just plan terraform, don't apply

```bash
make plan-only
# Then manually:  cd gke_terraform && terraform apply tfplan
```

### Daily operations

```bash
make scale-up          # start of session — bring nodes back from 0
make portforward       # open UIs (ArgoCD, Kargo, Grafana, Prom, Loki)
# ... work ...
make portforward-stop
make scale-down        # end of session — save cost
```

---

## Design notes

- **Each script is independently runnable** — `./04-platform.sh` works without going through the orchestrator (provided env is set / `config.env` is sourced).
- **`lib.sh` provides** colored logging, `run` (DRY_RUN-aware), `wait_for` (with timeout), `secret_exists`, `require_env`.
- **Failures fail fast** — no silent retries. The migration session's auto-retry on the WI race was helpful one time but masked the root cause. Better: fix it once in `eso.tf` and surface errors otherwise.
- **No hardcoded GCP project anywhere** — everything reads from env vars set by `config.env`.

---

## Future improvements (not in this version)

- `make destroy-old SOURCE=<project>` — automate teardown of old project after migration
- Kargo `Role` CRD auto-application for the K8s user (currently UI-only)
- Multi-region support (currently single zone)
- Optional Chaos Mesh install as Phase 06.5 (currently deferred)
