#!/usr/bin/env bash
# Chaos-gate orchestrator. Runs as the `job` provider inside the Kargo
# `chaos-gate` AnalysisTemplate. Its EXIT CODE is the promotion verdict.
#
# Where it runs: the pod lands in `url-shortener` (the Kargo project ns, where
# Kargo creates the AnalysisRun). So every kubectl call targets
# `url-shortener-staging` EXPLICITLY — the pod's default ns is url-shortener,
# and omitting -n would silently hit the wrong namespace.
#
# What `Accomplished` means: the Workflow reaching Accomplished only says the
# faults fired and the trailing `settle` Suspend let metrics scrape. It does NOT
# mean the app survived — PASS/FAIL comes solely from score_experiment.py below.
#
# Image requirement (decided when wiring the AnalysisTemplate): needs bash +
# kubectl + python3. score_experiment.py uses only stdlib (urllib), so no pip.
#
# NOTE on `set -e`: deliberately omitted. Step 6 must keep scoring after a
# failing experiment to print the full scorecard; -e would abort on the first
# nonzero scorer exit. We use -uo pipefail and guard scorers with `|| rc=1`.
set -uo pipefail

NS=url-shortener-staging
GATE_LABEL="app=chaos-gate"            # fixed label on every gate workflow (prune key)
# In-cluster the CM mounts at /scripts. Override (SCRIPTS=$PWD, plus PROM_URL for
# score_experiment.py) to run this SAME script from a laptop for debugging/game-days.
SCRIPTS="${SCRIPTS:-/scripts}"
EXPERIMENTS=(postgres redis signer)    # workflow templateNames == scorer key prefixes

# Scoring window per experiment = duration + settle. Set to the per-node
# deadlines in workflow.yaml (inside a Workflow the chaos `duration` field is
# ignored — the fault lasts until the node deadline).
# Over-wide windows are SAFE for the verdict: each check is dependency-scoped
# (dependency_up{postgres}, redis p95, etc.) and the gate is an OR, so a wider
# window can't manufacture a false PASS. Its only cost is diagnostic
# ATTRIBUTION — a neighbor's fault landing in the bleed zone could show its ✗ on
# the wrong experiment's row. (Confirm no check can spuriously FAIL on width:
# diff wide-vs-narrow observed values via `score_experiment.py --prom` against a
# real run. See LEARNINGS 6.3.)
declare -A DURATION=( [postgres]=120 [redis]=90 [signer]=90 )

# ── Step 1: prune prior runs ──────────────────────────────────────────────────
# generateName → no predictable name, so prune by the fixed label. Deleting a
# Workflow cascades (owner refs) to its workflownodes + spawned PodChaos.
echo ">> pruning prior gate workflows"
kubectl -n "$NS" delete workflow -l "$GATE_LABEL" --ignore-not-found

# ── Step 2: fire loadgen ──────────────────────────────────────────────────────
# The Workflow's own 90s `warmup` Suspend absorbs k6 ramp, so we fire-and-forget
# here. loadgen Job self-cleans via ttlSecondsAfterFinished.
LOADGEN="loadgen-$(date +%s)"
echo ">> starting loadgen: $LOADGEN"
kubectl -n "$NS" create job --from=cronjob/loadgen "$LOADGEN"

# ── Step 3: create the workflow, capture the generateName'd name ──────────────
WF=$(kubectl -n "$NS" create -f "$SCRIPTS/workflow.yaml" -o jsonpath='{.metadata.name}')
echo ">> created workflow: $WF"

# ── Step 4: wait for faults to finish + metrics to settle ─────────────────────
echo ">> waiting for $WF to reach Accomplished (<=15m)"
if ! kubectl -n "$NS" wait --for=condition=Accomplished "workflow/$WF" --timeout=900s; then
  echo "!! workflow did not Accomplish within timeout — scoring whatever data exists"
fi

# ── Step 5: extract per-experiment inject times (node .spec.startTime) ────────
# Select THIS run's nodes by the per-run workflow label, map templateName→startTime.
# startTime is already YYYY-MM-DDTHH:MM:SSZ — exactly what --inject-at parses.
declare -A T
while read -r tname tstart; do
  [ -n "$tname" ] && T[$tname]=$tstart
done < <(
  kubectl -n "$NS" get workflownode -l "chaos-mesh.org/workflow=$WF" \
    -o jsonpath='{range .items[*]}{.spec.templateName}{" "}{.spec.startTime}{"\n"}{end}'
)

# ── Step 6: score each experiment; ANY fail → gate fails ──────────────────────
rc=0
for exp in "${EXPERIMENTS[@]}"; do
  if [ -z "${T[$exp]:-}" ]; then
    echo "!! no startTime captured for '$exp' — cannot score, failing gate"
    rc=1
    continue
  fi
  echo ">> scoring $exp (inject=${T[$exp]} duration=${DURATION[$exp]}s)"
  python3 "$SCRIPTS/score_experiment.py" "${exp}-pod-failure" \
    --inject-at "${T[$exp]}" --duration "${DURATION[$exp]}" || rc=1
done

# ── (optional, deferred) Grafana region-annotation of the run window ──────────
# LEARNINGS:1036 wants the gate to POST a Grafana annotation marking the run.
# Left out of the verdict path to keep the gate focused; add as a non-fatal hook
# once a Grafana SA token is available. Not implemented on purpose.

# ── Step 7: verdict ───────────────────────────────────────────────────────────
echo ">> GATE VERDICT: $([ $rc -eq 0 ] && echo PASS || echo FAIL)"
exit $rc
