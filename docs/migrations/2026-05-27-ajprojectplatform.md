# GCP Project Migration: `amoghdevops` → `ajprojectplatform`

**Date**: 2026-05-27
**Duration**: ~5 hours (one session)
**Status**: ✅ Verified end-to-end (loadgen 100% success across 298 iterations)
**Old project**: Untouched, pending teardown (credits expired — degrading on its own)

---

## TL;DR

The free GCP credits on `amoghdevops` (project number `241566214193`) expired. We provisioned a new GCP project `ajprojectplatform` (`382767058620`) and migrated the entire `k8s-chaos-promotion` stack — GKE cluster, GAR, Workload Identity Federation, ArgoCD, Kargo, ESO, observability, the URL-shortener app, and the signer-backed loadgen — to it. Same cluster name, same node pool names, same service-account names, same secret values (byte-identical via SHA256 verification). Only project ID / project number / derived values changed (SA emails, WIF provider path, GAR URLs, GCS state bucket).

The old project stays live and untouched throughout. After verification, end-to-end loadgen ran with 100% success: 298 iterations, 0 failures across 898 requests, HTTP p95 = 1.29s, tx confirmation p95 = 1.35s — actually **better** than the original Phase 5.5 baseline (94% shorten 201, 99.7% tx success).

---

## Context

- **Old project**: `amoghdevops` (`241566214193`) — GCP free trial credits expired, no payment method attached
- **New project**: `ajprojectplatform` (`382767058620`) — fresh free trial under a different account (`blrmaganee@gmail.com`)
- **Old project secret access**: required switching to `amoghjay.us@gmail.com` account (the owner of `amoghdevops`)
- **Constraint**: keep all secret values, SA names, cluster name, node pool names identical; only project ID, project number, and derived values change

---

## What stayed identical

| Item | Value |
|---|---|
| Cluster name | `chaos-promotion` |
| Region / zone | `us-central1` / `us-central1-a` |
| Node pool names | `default-pool`, `chaos-pool` |
| Service account names | `gke-node-sa`, `external-secrets-sa`, `github-actions-sa` |
| K8s namespaces | (all 12 unchanged) |
| GitHub repo | `amoghjay/k8s-chaos-promotion` |
| Secret names | (all 10 unchanged, values byte-identical) |
| WIF pool / provider IDs | `github-actions-pool` / `github-provider` |
| Helm release names | `cert-manager`, `argocd`, `argo-rollouts`, `external-secrets`, `kargo` |

## What changed (mechanical)

| Variable | Old | New |
|---|---|---|
| Project ID | `amoghdevops` | `ajprojectplatform` |
| Project number | `241566214193` | `382767058620` |
| TF state bucket | `amoghdevops-tf-state` | `ajprojectplatform-tf-state` |
| SA email pattern | `*@amoghdevops.iam.gserviceaccount.com` | `*@ajprojectplatform.iam.gserviceaccount.com` |
| WIF provider path | `projects/241566214193/...` | `projects/382767058620/...` |
| GAR image refs | `us-central1-docker.pkg.dev/amoghdevops/...` | `us-central1-docker.pkg.dev/ajprojectplatform/...` |
| ESO ClusterSecretStore `projectID` | `amoghdevops` | `ajprojectplatform` |

**18 config files** edited via single `sed -i -e 's|amoghdevops|ajprojectplatform|g' -e 's|241566214193|382767058620|g'` pass — 36 lines changed total.

---

## Phase-by-phase

### Phase A — Pre-flight

1. **APIs enabled** on new project (one `gcloud services enable` call):
   `compute`, `container`, `artifactregistry`, `secretmanager`, `iamcredentials`, `sts`, `iam`

2. **TF state bucket** (created manually before this session):
   ```bash
   gcloud storage buckets create gs://ajprojectplatform-tf-state \
     --project=ajprojectplatform --location=us-central1 \
     --uniform-bucket-level-access
   gcloud storage buckets update gs://ajprojectplatform-tf-state --versioning
   ```

3. **Secret migration** (10 secrets streamed old→new, no disk writes):
   ```bash
   for s in database-password github-pat grafana-admin-password \
            radius-rpc-url-{testnet,mainnet} \
            service-wallet-address-{testnet,mainnet} \
            load-test-wallet-key-{1,2,3}; do
     gcloud secrets versions access latest --secret="$s" \
       --project=amoghdevops --account=amoghjay.us@gmail.com \
     | gcloud secrets create "$s" --project=ajprojectplatform \
       --replication-policy=automatic --data-file=- > /dev/null
   done
   ```
   Then SHA256-compared each value old vs new — all 10 matched.

4. **File rewrites** — 17 files (later 18, see Gotcha #1) edited via `sed`. Diff stat: 36 insertions(+), 36 deletions(-).

### Phase B — Terraform

```bash
cd gke_terraform
rm -rf .terraform/       # purge old GCS backend cache
terraform init           # configures new GCS backend
terraform plan -out=tfplan
terraform apply tfplan
```

**Hit Gotcha #1** (WI pool race) on first apply. Fixed by adding `depends_on = [google_container_cluster.gke_cluster]` to the ESO Workload Identity binding in `eso.tf`. Re-ran `terraform apply` — succeeded with `1 added, 0 changed, 0 destroyed`.

**Outputs verified**:
- cluster_endpoint, cluster_name, all 3 SA emails, GAR URL, WIF provider path with new project number

Switched kubectl context:
```bash
gcloud container clusters get-credentials chaos-promotion \
  --zone us-central1-a --project ajprojectplatform
# context: gke_ajprojectplatform_us-central1-a_chaos-promotion
```

### Phase C — Helm installs (5 things, order matters)

```bash
# 1. cert-manager (Kargo hard dep — install with --wait)
helm install cert-manager jetstack/cert-manager \
  -n cert-manager --create-namespace \
  --set crds.enabled=true --wait

# 2. ArgoCD
helm install argocd argo/argo-cd -n argocd --create-namespace

# 3. Argo Rollouts (CRDs for Kargo AnalysisTemplates)
helm install argo-rollouts argo/argo-rollouts -n argo-rollouts --create-namespace

# 4. ESO with WIF annotation (NEW SA email — critical edit)
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace \
  --set serviceAccount.annotations."iam\.gke\.io/gcp-service-account"=external-secrets-sa@ajprojectplatform.iam.gserviceaccount.com \
  --wait

# 5. Kargo (OCI repo — charts.kargo.io is dead per CLAUDE.md)
read -s "KARGO_PW?Set Kargo admin password: " && echo
export KARGO_PW_HASH=$(htpasswd -bnBC 10 "" "$KARGO_PW" | tr -d ':\n')
export KARGO_TOKEN_KEY=$(openssl rand -base64 29 | tr -d '=+/' | cut -c1-32)
unset KARGO_PW   # ← caused Gotcha #7 — see below

helm install kargo oci://ghcr.io/akuity/kargo-charts/kargo \
  -n kargo --create-namespace \
  --set api.adminAccount.passwordHash="$KARGO_PW_HASH" \
  --set api.adminAccount.tokenSigningKey="$KARGO_TOKEN_KEY" \
  --wait
```

**Note**: LEARNINGS.md ordering puts cert-manager LAST, but functionally any order that puts cert-manager before Kargo works. I sequenced cert-manager first + `--wait` to remove the race risk entirely.

**ESO is imperative, not GitOps-managed** — chicken-and-egg with ArgoCD repo auth (ArgoCD needs a Git secret created by ESO, so ESO can't be installed by ArgoCD). See Gotcha #11.

### Phase D — Local applies (no Git fetching)

```bash
kubectl apply -f kubernetes/bootstrap/secrets/cluster-secret-store.yaml
kubectl get clustersecretstore gcp-secret-manager   # → STATUS: Ready

kubectl apply -f kubernetes/bootstrap/secrets/external-secrets-argocd.yaml
kubectl get secret repo-k8s-chaos-promotion -n argocd   # ArgoCD discovers via label
```

ArgoCD now has Git auth via the ESO-synced GitHub PAT.

### Phase E — Git push + GitOps + Kargo CRs

1. **Add CI trigger comments** (the workflows have `paths:` filters; infra-only commits won't fire):
   - `app/main.py` — comment line → triggers `build-push.yaml`
   - `signer/main.py` — comment line → triggers `build-radius-signer.yaml`
   - `docker/k6-ethereum/Dockerfile` — comment line → triggers `build-k6-ethereum.yaml`

2. **Commit + push to PR**:
   ```bash
   git add app/main.py signer/main.py docker/k6-ethereum/Dockerfile \
           .github/workflows/* gke_terraform/* helm/url-shortener/values.yaml \
           kubernetes/bootstrap/_pending/eso.yaml \
           kubernetes/bootstrap/secrets/cluster-secret-store.yaml \
           kubernetes/jobs/{kustomization,radius-signer,radius-tps-bench-job}.yaml \
           kubernetes/kargo/{promotiontask-*,stage-*,warehouse}.yaml
   git commit -m "chore(infra): migrate from GCP project amoghdevops to ajprojectplatform"
   git push -u origin migration/new_acc
   gh pr create --base main --title "migrate: amoghdevops → ajprojectplatform" --body ...
   ```

3. **Hit merge conflicts** in `kubernetes/jobs/kustomization.yaml` — CI auto-promotions on main during the branch life bumped `sha-` tags. Resolution rule: **keep new project path from branch, take newer tag from main**. (See Gotcha #3.)

4. **Merge PR** → 3 CI workflows fired in parallel:
   - `build-push.yaml` → url-shortener image to new GAR
   - `build-radius-signer.yaml` → radius-signer image to new GAR (auto-commits updated kustomization back to main)
   - `build-k6-ethereum.yaml` → k6-ethereum image to new GAR (same auto-commit)

5. **Verify images in new GAR**:
   ```bash
   gcloud artifacts docker images list \
     us-central1-docker.pkg.dev/ajprojectplatform/k8s-chaos-demo/<name> \
     --include-tags --project=ajprojectplatform
   ```

6. **Apply root-app**:
   ```bash
   git checkout main && git pull
   kubectl apply -f kubernetes/argocd/root-app.yaml
   ```
   Spawns child apps: `observability` (kube-prometheus-stack + Loki) and `chaos-jobs` (radius-signer + loadgen).

7. **Grafana ExternalSecret manual gap** — Grafana pod stuck in `CreateContainerConfigError` because `grafana-admin-secret` didn't exist. Applied `kubernetes/bootstrap/secrets/external-secrets-monitoring.yaml` manually. See Gotcha #5.

8. **Apply Kargo resources** (order matters: project first creates ns):
   ```bash
   kubectl apply -f kubernetes/kargo/project.yaml
   kubectl apply -f kubernetes/kargo/projectconfig.yaml
   kubectl apply -f kubernetes/kargo/credentials-git.yaml
   kubectl apply -f kubernetes/kargo/analysistemplate.yaml
   kubectl apply -f kubernetes/kargo/stage-{dev,staging,prod}.yaml
   kubectl apply -f kubernetes/kargo/warehouse.yaml
   ```
   **Skipped** `promotiontask-*.yaml` — legacy, replaced by inlined Stage steps. See Gotcha #12.

9. **Apply ApplicationSet**:
   ```bash
   kubectl apply -f kubernetes/apps/applicationset.yaml
   ```
   Generates 3 ArgoCD apps: `url-shortener-{dev,staging,prod}`.

10. **Hit Kargo step-6 error** — `unable to find Argo CD Application "url-shortener-dev"`. Sequencing bug: I applied Kargo Warehouse before ApplicationSet, so when Warehouse created Freight and auto-promoted to dev, step-6 (`argocd-update`) couldn't find the destination app. See Gotcha #4.

11. **Hit Kargo authz denial** — deleting the failed Promotion via `kubectl delete` was rejected by Kargo's admission webhook (subject `blrmaganee@gmail.com` not permitted). Forgotten password from C5 blocked UI login. See Gotchas #6 + #7.

12. **Reset Kargo password** via `helm upgrade --reuse-values --set api.adminAccount.passwordHash=...` + `kubectl rollout restart deploy/kargo-api`.

13. **Logged into Kargo UI** at `https://localhost:8081` (port 443, not 80 as CLAUDE.md claimed — see Gotcha #8). Retried promotion → succeeded.

14. **Force-synced chaos-jobs** which was OutOfSync because `url-shortener-staging` ns didn't exist on first sync attempt:
    ```bash
    kubectl patch app chaos-jobs -n argocd --type merge -p '{"operation":{"sync":{}}}'
    ```

15. **Manually promoted dev → staging** in Kargo UI (per `autoPromotionEnabled: false` for staging).

16. **Re-ran loadgen** by deleting the failed Job + re-syncing chaos-jobs. Got 100% success — see Verification section.

---

## Gotchas (root causes + fixes)

### 1. Terraform Workload Identity Pool race condition

**Symptom** (during `terraform apply`):
```
Error: Error applying IAM policy for service account 'external-secrets-sa@ajprojectplatform...':
googleapi: Error 400: Identity Pool does not exist (ajprojectplatform.svc.id.goog).
```

**Root cause**: The `external-secrets-sa` Workload Identity binding tries to bind to `<project>.svc.id.goog[ns/sa]`, but that pool only exists once a GKE cluster with `workload_identity_config` is created. Without an explicit `depends_on`, Terraform attempts the binding in parallel with cluster creation — sometimes wins, sometimes loses.

**Fix**: Add `depends_on` to the binding:

```hcl
# gke_terraform/eso.tf
resource "google_service_account_iam_member" "eso_workload_identity_binding" {
  service_account_id = google_service_account.external_secrets.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[external-secrets/external-secrets]"
  depends_on         = [google_container_cluster.gke_cluster]   # ← added
}
```

Re-run `terraform apply` — picks up the binding alone (1 to add).

### 2. CI workflows don't trigger on infra-only commits

**Symptom**: PR merged to main, no CI builds fire. Kargo Warehouse polls new (empty) GAR and finds no images.

**Root cause**: All three workflows have path filters: `paths: ["app/**"]`, `paths: ["signer/**"]`, `paths: ["docker/k6-ethereum/**"]`. The migration commit only touches `.github/workflows/*`, `kubernetes/*`, `gke_terraform/*`, `helm/url-shortener/values.yaml` — none of those filtered paths.

**Fix**: Add one-line comments inside each filtered directory (we used `# Migrated to GCP project ajprojectplatform on 2026-05-27`). All three workflows fire on the next push.

**Alternative**: `gh workflow run <name>.yaml --ref main` works for `build-radius-signer` and `build-k6-ethereum` (they have `workflow_dispatch:`). `build-push.yaml` does NOT — would need the workflow edited too.

### 3. Merge conflicts from CI auto-promotions on main

**Symptom**: PR merge blocked by conflicts in `kubernetes/jobs/kustomization.yaml` (radius-signer + k6-ethereum tag entries).

**Root cause**: CI workflows on main auto-commit updated `kustomization.yaml` with new tags after each build. While we were on `migration/new_acc`, main got bumped. Same line conflicts.

**Fix**: Manual resolution — **keep new project path from branch (`ajprojectplatform`), take newer tag from main (`sha-7805120`)**.

Don't blindly "Accept current" or "Accept incoming" — both lose something. Editor + manual merge required.

### 4. ApplicationSet must precede Kargo Warehouse

**Symptom**: Kargo dev Stage shows:
```
step "step-6" met error threshold of 1: error running step "step-6":
unable to find Argo CD Application "url-shortener-dev" in namespace "argocd"
```

**Root cause**: Stage's promotion template has 6 steps. Step 6 (`argocd-update`) tries to trigger a sync on the ArgoCD Application named `url-shortener-${{ ctx.stage }}`. If the ApplicationSet hasn't been applied yet, that Application doesn't exist. Kargo's controller errors past its retry threshold and stops.

**Fix**: Apply ApplicationSet **before** Warehouse:

```
Order (corrected):
1. kubectl apply -f kubernetes/kargo/{project,projectconfig,credentials-git,analysistemplate}.yaml
2. kubectl apply -f kubernetes/kargo/stage-{dev,staging,prod}.yaml
3. kubectl apply -f kubernetes/apps/applicationset.yaml     ← BEFORE Warehouse
4. kubectl apply -f kubernetes/kargo/warehouse.yaml
```

To recover from this error after the fact: delete the failed Promotion (requires Kargo authz — see Gotcha #6).

### 5. Grafana ExternalSecret has to be applied manually after root-app

**Symptom**: `observability-grafana-*` pod stuck in `CreateContainerConfigError`. `kubectl describe pod` shows: `Error: secret "grafana-admin-secret" not found`.

**Root cause**: `kubernetes/bootstrap/secrets/external-secrets-monitoring.yaml` is in the `bootstrap/secrets/` directory — **explicitly outside the path watched by `root-app`** (see CLAUDE.md note: "manually applied, NOT managed by root-app"). The `monitoring` namespace doesn't exist before root-app runs, so it couldn't be applied earlier.

**Fix**: After root-app creates `monitoring` ns (during observability sync), apply manually:

```bash
kubectl apply -f kubernetes/bootstrap/secrets/external-secrets-monitoring.yaml
kubectl get externalsecret -n monitoring grafana-admin   # SecretSynced
kubectl delete pod -n monitoring -l app.kubernetes.io/name=grafana   # force restart
```

Same pattern as the ArgoCD repo secret in Phase D — both are "manual gap" ExternalSecrets that must be applied between operator install and consuming app starting.

### 6. Kargo admission webhook denies kubectl operations

**Symptom**:
```
Error from server (Forbidden): admission webhook "promotion.kargo.akuity.io" denied the request:
Promotion.kargo.akuity.io "dev.<id>" is forbidden:
subject "blrmaganee@gmail.com" is not permitted to delete Promotions for Stage "dev"
```

**Root cause**: Kargo has its own authorization layer on top of K8s RBAC. K8s users (the identity in your `kubectl` context) are not automatically Kargo project admins. Kargo evaluates the subject against `rbac.kargo.akuity.io/v1alpha1 Role` resources within the project namespace. Without a binding, K8s users have **no** Kargo permissions.

**Fix (immediate)**: Use the Kargo UI, log in as the admin account (set during install).

**Fix (permanent)**: Apply a Kargo `Role` binding your K8s user:

```yaml
apiVersion: rbac.kargo.akuity.io/v1alpha1
kind: Role
metadata:
  name: <user>-project-admin
  namespace: url-shortener
subjects:
- kind: User
  name: <user-email>
rules:
- resources: ["promotions", "stages", "freights", "warehouses"]
  verbs: ["*"]
```

### 7. Kargo admin password lost (shell unset)

**Symptom**: Trying to log into Kargo UI to recover from Gotcha #6 — don't remember password. Phase C5 step `unset KARGO_PW` wiped plaintext from shell.

**Root cause**: bcrypt is one-way. The hash stored by Helm cannot be reversed. Saving to a password manager wasn't part of the C5 step.

**Fix**: Reset via `helm upgrade` + restart:

```bash
read -s "KARGO_PW?New Kargo admin password: " && echo   # zsh syntax!
export KARGO_PW_HASH=$(htpasswd -bnBC 10 "" "$KARGO_PW" | tr -d ':\n')
unset KARGO_PW

helm upgrade kargo oci://ghcr.io/akuity/kargo-charts/kargo \
  -n kargo --reuse-values \
  --set api.adminAccount.passwordHash="$KARGO_PW_HASH" --wait

kubectl rollout restart -n kargo deploy/kargo-api
```

`--reuse-values` preserves the existing `tokenSigningKey` (you don't want to regenerate that — invalidates all existing sessions/tokens).

### 8. Kargo service port is 443, not 80

**Symptom**: `kubectl port-forward svc/kargo-api -n kargo 8081:80` errors with: `Service kargo-api does not have a service port 80`.

**Root cause**: CLAUDE.md says port 80, based on an older chart version. The current chart (≥ v1.9.x) exposes only port 443.

**Fix**: Use `8081:443` and **https** (not http) in browser:

```bash
kubectl port-forward svc/kargo-api -n kargo 8081:443
# Browser: https://localhost:8081  (accept the self-signed cert warning)
```

To check generally: `kubectl get svc kargo-api -n kargo -o jsonpath='{.spec.ports}' | jq`.

### 9. zsh `read -p` syntax differs from bash

**Symptom**: `read -s -p "..." VAR` returns `read: -p: no coprocess`.

**Root cause**: `read -p` is bash-only. zsh uses `read "VAR?prompt"`.

**Fix**:
```zsh
read -s "VAR?Prompt: " && echo
```

For migrations on Mac, default shell is zsh — this trips up copy-pasted bash snippets.

### 10. Loadgen Job marks chaos-jobs Degraded

**Symptom**: After applying chaos-jobs, ArgoCD shows `chaos-jobs: Synced + Degraded`.

**Root cause**: `loadgen-job.yaml` runs k6 against `url-shortener-staging`. k6 exits non-zero when its thresholds (>90% shorten 201, >95% redirect, etc.) aren't met. ArgoCD marks a Job that exits non-zero as `Degraded`. If staging app pods are in `ImagePullBackOff` (because values-staging.yaml has a stale tag from before the migration) loadgen has no target → all checks fail → Job exits 1 → app Degraded.

**Fix**: Wait for the pipeline to promote a valid image into staging. Then re-run:

```bash
kubectl delete job loadgen -n url-shortener-staging
kubectl patch app chaos-jobs -n argocd --type merge -p '{"operation":{"sync":{}}}'
```

The chaos-jobs app re-creates the Job from Git, runs against healthy staging, exits 0 → `Healthy`.

**This is documented in LEARNINGS.md line 623** — not a migration-specific bug, just a re-encounter of an existing known behavior.

### 11. `_pending/eso.yaml` is dead code

**Symptom**: Why is there an ArgoCD Application file for ESO in `kubernetes/bootstrap/_pending/eso.yaml` but ESO isn't managed by GitOps?

**Root cause (1 — historical)**: The `_pending/` directory was meant to hold "pre-built but not yet committed" Apps. observability.yaml was promoted out of `_pending/`; eso.yaml and chaos-mesh.yaml never were.

**Root cause (2 — structural)**: Even if you tried to promote eso.yaml, you can't usefully manage ESO via ArgoCD because of a chicken-and-egg:
- ArgoCD needs Git auth → secret created by ESO
- ESO must run first → ArgoCD would install ESO
- ArgoCD needs Git auth → (circular)

**Root cause (3 — chart version)**: The file references `targetRevision: "2.2.0"` — that version doesn't exist for `external-secrets/external-secrets` chart (real versions are `0.10.x`–`0.14.x`). Would fail to apply even if attempted.

**Fix**: Delete the file. Keep ESO as a permanent imperative install. Documented in CLAUDE.md.

### 12. `promotiontask-*.yaml` files are legacy

**Symptom**: Two files (`promotiontask-dev.yaml`, `promotiontask-promote.yaml`) in `kubernetes/kargo/` that aren't referenced by any Stage.

**Root cause**: Per LEARNINGS.md v6 update: Kargo v1.9.5 prefixes PromotionTask step aliases with `task-1::` in state. `outputs.<alias>.<field>` lookups inside the PromotionTask context return nil. The workaround was to inline all steps directly into each Stage's `promotionTemplate.spec.steps`, removing the PromotionTask layer entirely.

The PromotionTask files were never deleted, just abandoned.

**Fix**: Delete the files. Stages already inline their full step list (see `kubernetes/kargo/stage-dev.yaml` lines 20-65).

---

## Verification

### End-to-end loadgen result (proves real-traffic correctness)

Ran after staging promotion completed:

```
checks_total..............: 596    1.965882/s
checks_succeeded..........: 100.00% (596/596)
shorten_201_rate..........: 100.00% (298/298)
redirect_ok_rate..........: 100.00% (298/298)
tx_submit_success_rate....: 100.00% (298/298)
tx_receipt_success_rate...: 100.00% (298/298)
shorten_402_rate..........: 0.00%   (0/298)
shorten_409_rate..........: 0.00%   (0/298)
shorten_5xx_rate..........: 0.00%   (0/298)
http_req_failed...........: 0.00%   (0/898)

http_req_duration p95:    1.29s
tx_confirmation_ms p95:   1.35s

Total: 298 complete iterations, 3 VUs, 5 minutes
```

**Better than Phase 5.5 baseline** (94% shorten 201, 99.7% tx success). Real SBC ERC-20 transfers on Radius testnet, signer-backed flow, no faucet bottleneck.

### Resource state

- **6/6 ArgoCD apps**: `Synced + Healthy` after loadgen retry
- **7/7 ExternalSecrets**: all `SecretSynced=True`
- **Kargo Stages**: dev + staging `Healthy` with same Freight (the migration merge commit), prod gated for manual
- **All platform pods Running**: cert-manager, ArgoCD, Argo Rollouts, ESO, Kargo, kube-prometheus-stack, Loki, Promtail
- **App pods**: url-shortener {dev/staging/prod} as designed (dev/staging running new image, prod waiting on manual promotion)

---

## Deferred cleanup (for the bootstrap-automation session)

1. **Delete dead files**:
   - `kubernetes/kargo/promotiontask-dev.yaml`
   - `kubernetes/kargo/promotiontask-promote.yaml`
   - `kubernetes/bootstrap/_pending/eso.yaml`
   - `kubernetes/kargo-values.yaml` (970 lines of upstream defaults, never consumed)

2. **Remove migration-trigger comments** (single-use, served their purpose):
   - `app/main.py` first line after docstring
   - `signer/main.py` first line after docstring
   - `docker/k6-ethereum/Dockerfile` first comment line

   For future migrations the bootstrap-automation script can use `gh workflow run` (for the two workflows that support `workflow_dispatch`) and add it to `build-push.yaml` if needed — cleaner than committing fake comments.

3. **Apply Kargo `Role`** binding `blrmaganee@gmail.com` to project-admin verbs on `url-shortener` namespace. (Currently using UI; CLI ops blocked by admission webhook.)

4. **Update CLAUDE.md**:
   - Project ID + project number to new values
   - Kargo port-forward note: `8081:80` → `8081:443` (https)
   - Add pointer: "Migration history under `docs/migrations/`"

5. **Old project teardown** (whenever — credits expired so it's degrading on its own):
   - `terraform destroy` from `amoghjay.us@gmail.com` account
   - `gcloud projects delete amoghdevops` (30-day pending window, recoverable)

---

## Reusable patterns (for the bootstrap automation)

These are the principles the `platform_setup_scripts/` package should encode:

1. **Phase ordering** — strict dependency chain:
   ```
   APIs → GCS bucket → secrets → terraform → kubectl context →
   cert-manager → ArgoCD → Argo Rollouts → ESO → Kargo →
   ClusterSecretStore → ArgoCD repo ES → Grafana ES (after monitoring ns) →
   Kargo project/projectconfig/credentials-git/analysistemplate/stages →
   ApplicationSet (before Warehouse!) → Warehouse →
   chaos-jobs sync → manual promotions
   ```

2. **Idempotency** — every step should be `describe || create`:
   ```bash
   gcloud secrets describe X --project=$P >/dev/null 2>&1 \
     || gcloud secrets create X --project=$P --replication-policy=automatic --data-file=-
   ```

3. **Secret streaming via pipe** (no disk):
   ```bash
   gcloud secrets versions access latest --secret=X --project=$OLD --account=$OLD_ACCT \
     | gcloud secrets create X --project=$NEW --replication-policy=automatic --data-file=-
   ```

4. **Verification before next step** — each phase emits a green/red signal; orchestrator gates on it.

5. **Project-agnostic** — all GCP IDs come from env vars (`PROJECT_ID`, `PROJECT_NUMBER`, `REGION`, `ZONE`), never hardcoded.

6. **Encode known gotchas**:
   - `depends_on` in `eso.tf` (gotcha 1)
   - Trigger-touches OR `gh workflow run` (gotcha 2)
   - Apply ApplicationSet before Warehouse (gotcha 4)
   - Apply Grafana ES after monitoring ns exists (gotcha 5)
   - Save Kargo password before unsetting (gotcha 7)
   - Use port 443 for Kargo (gotcha 8)
   - Use zsh-compatible `read` syntax (gotcha 9)

---

## Migration commit reference

- **PR**: <will-be-filled-after-merge>
- **Migration commit SHA**: `715085c…` (the freight that ran dev + staging)
- **Branch**: `migration/new_acc` (merged + can be deleted)
- **Files changed**: 18 config files (36 lines) + 3 trigger files

---

## Time accounting (rough)

| Phase | Time |
|---|---|
| A (pre-flight: APIs, bucket, secrets, file edits) | ~30 min |
| B (terraform: plan, race, fix, apply) | ~30 min |
| C (helm installs) | ~15 min |
| D (cluster secrets) | ~5 min |
| E (commit, CI, root-app, Kargo CRs, ApplicationSet, debugging) | ~3 hours |
| Verification (promotions, loadgen) | ~30 min |
| **Total** | **~5 hours** |

Bootstrap automation target: reduce E + verification to ~30 min total for the next migration.
