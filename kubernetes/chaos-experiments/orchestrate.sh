#!/usr/bin/env bash
# Chaos-gate orchestrator. Runs as the Kargo AnalysisTemplate `job` provider;
# the exit code is the promotion verdict. The Workflow reaching Accomplished
# only means the faults fired — PASS/FAIL comes from score_experiment.py.
#
# No `set -e`: scoring must continue past a failing experiment so the full
# scorecard prints; scorer failures are collected via `|| rc=1`.
set -uo pipefail

# The pod runs in url-shortener but acts on url-shortener-staging, so every
# kubectl call passes -n explicitly.
NS=url-shortener-staging
GATE_LABEL="app=chaos-gate"            # fixed label on every gate workflow (prune key)
SCRIPTS="${SCRIPTS:-/scripts}"         # CM mount in-cluster; set SCRIPTS=$PWD (+ PROM_URL) to run locally
EXPERIMENTS=(postgres redis signer)    # workflow templateNames == scorer key prefixes

# Scoring window per experiment = duration + settle. Must match the per-node
# deadlines in workflow.yaml (inside a Workflow the fault lasts until the node
# deadline, not the chaos `duration`).
declare -A DURATION=( [postgres]=120 [redis]=90 [signer]=90 )

# Workflows use generateName, so prune prior runs by the fixed label. Deleting
# a Workflow cascades (owner refs) to its workflownodes and spawned PodChaos.
echo ">> pruning prior gate workflows"
kubectl -n "$NS" delete workflow -l "$GATE_LABEL" --ignore-not-found

# A podchaos stuck Terminating on the chaos-mesh/records finalizer keeps its
# fault active past the window, and the cascade delete misses already-orphaned
# objects — delete leftovers, then force-clear finalizers on anything stuck.
echo ">> sweeping leftover/orphaned chaos objects"
kubectl -n "$NS" delete podchaos -l chaos-mesh.org/workflow --ignore-not-found --wait=false 2>/dev/null
for pc in $(kubectl -n "$NS" get podchaos \
    -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.metadata.name}{" "}{end}' 2>/dev/null); do
  echo "   force-clearing stuck finalizer on podchaos/$pc"
  kubectl -n "$NS" patch podchaos "$pc" --type merge -p '{"metadata":{"finalizers":[]}}' 2>/dev/null
done

# Fire-and-forget: the workflow's 90s warmup Suspend absorbs the k6 ramp.
LOADGEN="loadgen-$(date +%s)"
echo ">> starting loadgen: $LOADGEN"
kubectl -n "$NS" create job --from=cronjob/loadgen "$LOADGEN"

WF=$(kubectl -n "$NS" create -f "$SCRIPTS/workflow.yaml" -o jsonpath='{.metadata.name}')
echo ">> created workflow: $WF"

# Poll for Accomplished instead of `kubectl wait`: wait's first GET can hit a
# lagging API-server replica right after the create and bail on NotFound
# without retrying; a poll loop tolerates the transient miss.
echo ">> waiting for $WF to reach Accomplished (<=15m)"
acc=""
for _ in $(seq 1 180); do            # 180 * 5s = 900s
  acc=$(kubectl -n "$NS" get workflow "$WF" \
    -o jsonpath='{.status.conditions[?(@.type=="Accomplished")].status}' 2>/dev/null)
  [ "$acc" = "True" ] && break
  sleep 5
done
if [ "$acc" != "True" ]; then
  echo "!! workflow did not Accomplish within timeout — scoring whatever data exists"
fi

# Map templateName -> node .spec.startTime: the per-experiment inject times.
declare -A T
while read -r tname tstart; do
  [ -n "$tname" ] && T[$tname]=$tstart
done < <(
  kubectl -n "$NS" get workflownode -l "chaos-mesh.org/workflow=$WF" \
    -o jsonpath='{range .items[*]}{.spec.templateName}{" "}{.spec.startTime}{"\n"}{end}'
)

# Score each experiment; any failure fails the gate. Track which failed so the
# Grafana annotation can name them.
rc=0
failed=""
for exp in "${EXPERIMENTS[@]}"; do
  if [ -z "${T[$exp]:-}" ]; then
    echo "!! no startTime captured for '$exp' — cannot score, failing gate"
    rc=1; failed="${failed:+$failed,}$exp"
    continue
  fi
  echo ">> scoring $exp (inject=${T[$exp]} duration=${DURATION[$exp]}s)"
  if ! python3 "$SCRIPTS/score_experiment.py" "${exp}-pod-failure" \
    --inject-at "${T[$exp]}" --duration "${DURATION[$exp]}"; then
    rc=1; failed="${failed:+$failed,}$exp"
  fi
done

# Diagnostic only — the verdict is already decided above. Wait (bounded) for
# loadgen to finish so its k6 summary lands in this log before the pod's TTL.
echo ">> waiting (<=4m) for loadgen $LOADGEN to finish, to capture its k6 summary"
for _ in $(seq 1 48); do
  conds=$(kubectl -n "$NS" get job "$LOADGEN" -o jsonpath='{range .status.conditions[*]}{.type}{" "}{end}' 2>/dev/null)
  case "$conds" in *Complete*|*Failed*) break;; esac
  sleep 5
done
echo "----- k6 loadgen summary ($LOADGEN) -----"
lgpod=$(kubectl -n "$NS" get pods -l batch.kubernetes.io/job-name="$LOADGEN" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$lgpod" ]; then
  kubectl -n "$NS" logs "$lgpod" 2>&1 | tail -45
else
  echo "!! loadgen pod gone (TTL-deleted or never scheduled) — no summary to show"
fi
echo "----- end k6 summary -----"

# Mark the run on the Grafana dashboard timeline. annotate.py self-skips
# without credentials and is wrapped so it can never change the verdict.
WF_START=$(kubectl -n "$NS" get workflow "$WF" -o jsonpath='{.status.startTime}' 2>/dev/null)
WF_END=$(kubectl -n "$NS" get workflow "$WF" -o jsonpath='{.status.endTime}' 2>/dev/null)
python3 "$SCRIPTS/annotate.py" \
  --verdict "$([ $rc -eq 0 ] && echo pass || echo fail)" \
  --failed "$failed" --start "$WF_START" --end "$WF_END" --workflow "$WF" || true

echo ">> GATE VERDICT: $([ $rc -eq 0 ] && echo PASS || echo FAIL)"
exit $rc
