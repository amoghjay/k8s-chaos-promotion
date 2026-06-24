#!/usr/bin/env python3
"""Chaos experiment scorer — query Prometheus over an experiment's window and
print a predicted-vs-observed PASS/FAIL scorecard. Exit 0 = all pass, 1 = any fail.

Two homes (same code):
  - manual / demo:
      python score_experiment.py redis-pod-failure  --inject-at 2026-06-19T16:09:12Z
  - 6.3 chaos-gate Job (in a ConfigMap): the exit code becomes the promotion verdict.

Run it from inside the cluster (so the Prometheus svc DNS resolves), e.g.:
  kubectl -n url-shortener-staging exec -i deploy/url-shortener-staging -- \
      python - < score_experiment.py  ... (or bake into the gate Job image/CM)

THRESHOLDS are the degraded-mode bars derived per experiment in LEARNINGS — looser
than steady-state SLOs (chaos *should* degrade things; the gate fails on collapse,
not on bumps). Tune the CHECKS table below; the harness is generic.
"""
import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# In-cluster default. Override with --prom or $PROM_URL to score against a
# port-forwarded Prometheus from a laptop (e.g. http://localhost:9090).
PROM = "http://observability-kube-prometh-prometheus.monitoring.svc:9090"
NS = "url-shortener-staging"

# ── Prometheus query harness (mechanical — generic, don't edit per experiment) ──
def query_range(expr, start, end, step="15s"):
    url = PROM + "/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": expr, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)["data"]["result"]

def query_instant(expr, t):
    url = PROM + "/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": t})
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)["data"]["result"]

def scalar(vec):
    # instant-vector result → one float. Counter exprs are wrapped in sum()/max(),
    # so there's ≤1 series; empty (metric never fired) == 0.
    if not vec:
        return 0.0
    try:
        f = float(vec[0]["value"][1])
    except (ValueError, KeyError, IndexError):
        return 0.0
    return 0.0 if math.isnan(f) else f

def _floats(series):
    out = []
    for s in series:
        for _, v in s["values"]:
            try:
                f = float(v)
            except ValueError:
                continue
            # float("NaN") does NOT raise — Prometheus returns "NaN" for e.g.
            # histogram_quantile over empty buckets, and one NaN poisons max().
            if not math.isnan(f):
                out.append(f)
    return out

def aggregate(series, how):
    vs = _floats(series)
    if not vs:                      # empty series == metric never fired
        return 0.0
    return {"max": max, "min": min,
            "avg": lambda x: sum(x) / len(x),
            "last": lambda x: x[-1]}[how](vs)

_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
        "==": lambda a, b: abs(a - b) < 1e-9}

# ── VACUOUS-PASS GUARD (attached to postgres + redis ONLY — see below) ───────────
# A real Kargo gate run once FALSE-PASSED: an orphaned chaos wedged the signer →
# loadgen aborted in preflight → 0 traffic → every SLO check passed trivially
# against no data → promotion green-lit untested. This guard turns that silent
# false-PASS into a loud ✗.
# TWO subtleties found empirically while tuning it:
#  1. `/livez` IS in http_requests_total (the liveness probe, ~35 reqs/window from
#     2 pods × 6/min) — a noise floor that's ALWAYS present. Must exclude it, else
#     a vacuous run reads ~35 reqs and the guard never fires. Hence handler!="/livez".
#  2. Do NOT attach this to the SIGNER experiment: when the signer is down loadgen
#     bails before /shorten, so the signer window legitimately sees ~0 user traffic
#     even in a healthy run — a guard there would false-FAIL. postgres + redis keep
#     serving under their faults, so they're the reliable "did load actually run?"
#     probes. Measured: real run postgres=429 redis=289 vs vacuous=0/0 → 50 is a
#     safe floor (huge margin both sides).
TRAFFIC_GUARD = {
    "name": "meaningful traffic flowed (guard vs vacuous pass)",
    "expr": 'sum(increase(http_requests_total{{namespace="{ns}", handler!="/livez"}}[{w}s]))',
    "op": ">", "threshold": 50, "unit": "reqs",
}

# ── PER-EXPERIMENT RULES  ***  TUNE THESE (degraded-mode thresholds)  *** ────────
# Each check: name, PromQL expr ({ns},{w} substituted), agg over the window, op, threshold.
#   - {w} is the scoring window length in seconds (duration + settle), for increase()/rate ranges.
#   - "must-stay-zero" counters use increase() over {w} with op "<" 0.5 (less than half an event).
EXPERIMENTS = {
    "postgres-pod-failure": {
        # PG recovery is SLOWER than redis: pod-failure forces a StatefulSet
        # restart + Bitnami init + WAL recovery (~25-30s) AFTER the 60s chaos
        # clears, plus the monitor's 10s detection lag. Validation run (17:20:45Z)
        # showed dependency_up{postgres} back to 1 only at inject+95s — so settle=30
        # closed the window 5s too early. 60s gives comfortable margin.
        "settle": 60,
        "checks": [
            TRAFFIC_GUARD,
            {"name": "0 app restarts (THE liveness-cascade detector — exp #1's bug)",
             "expr": 'max(increase(kube_pod_container_status_restarts_total{{namespace="{ns}", container="url-shortener"}}[{w}s]))',
             "agg": "max", "op": "<", "threshold": 0.5, "unit": "restarts"},
            {"name": "postgres outage actually landed (dependency_up flipped to 0)",
             "expr": 'min(url_shortener_dependency_up{{namespace="{ns}", dependency="postgres"}})',
             "agg": "min", "op": "==", "threshold": 0, "unit": "1/0"},
            {"name": "app recovered after PG returned (both pods dependency_up==1)",
             "expr": 'min(url_shortener_dependency_up{{namespace="{ns}", dependency="postgres"}})',
             "agg": "last", "op": "==", "threshold": 1, "unit": "1/0"},
            # Graceful-degradation check: PG-down must surface as a clean 503, NOT
            # an unhandled 500. Keyed on status="500" exactly so 503 passes. Uses
            # increase() → auto window-anchored (counts only this experiment's window).
            {"name": "PG-down degrades cleanly: /shorten 500 == 0 (503 is fine, 500 is a bug)",
             "expr": 'sum(increase(http_requests_total{{namespace="{ns}", handler="/shorten", status="500"}}[{w}s]))',
             "op": "<", "threshold": 0.5, "unit": "500s"},
        ],
    },
    "redis-pod-failure": {
        "settle": 40,   # seconds after the duration to keep scoring (pod recovery tail)
        "checks": [
            TRAFFIC_GUARD,
            {"name": "redirect p95 < 1.2s (fast-fail, not the old ~1s pin run-on)",
             "expr": 'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{namespace="{ns}", handler="/{{code}}"}}[1m])) by (le))',
             "agg": "max", "op": "<", "threshold": 1.2, "unit": "s"},
            {"name": "0 app restarts (redis not a liveness dep)",
             "expr": 'max(increase(kube_pod_container_status_restarts_total{{namespace="{ns}", container="url-shortener"}}[{w}s]))',
             "agg": "max", "op": "<", "threshold": 0.5, "unit": "restarts"},
            {"name": "redis outage actually landed (dependency_up flipped to 0)",
             "expr": 'min(url_shortener_dependency_up{{namespace="{ns}", dependency="redis"}})',
             "agg": "min", "op": "==", "threshold": 0, "unit": "1/0"},
        ],
    },
    "signer-pod-failure": {
        "settle": 40,
        "checks": [
            {"name": "no Permit2 replay: /shorten 409 == 0",
             "expr": 'sum(increase(http_requests_total{{namespace="{ns}", handler="/shorten", status="409"}}[{w}s]))',
             "agg": "max", "op": "<", "threshold": 0.5, "unit": "409s"},
            {"name": "no replay attempts caught at app layer",
             "expr": 'sum(increase(payment_replay_attempts_total{{namespace="{ns}"}}[{w}s]))',
             "agg": "max", "op": "<", "threshold": 0.5, "unit": "attempts"},
            {"name": "app is a bystander: 0 app restarts",
             "expr": 'max(increase(kube_pod_container_status_restarts_total{{namespace="{ns}", container="url-shortener"}}[{w}s]))',
             "agg": "max", "op": "<", "threshold": 0.5, "unit": "restarts"},
            {"name": "app never 5xx'd during the signer outage",
             "expr": 'sum(increase(http_requests_total{{namespace="{ns}", handler="/shorten", status=~"5.."}}[{w}s]))',
             "agg": "max", "op": "<", "threshold": 0.5, "unit": "5xx"},
        ],
    },
}

# ── Runner (mechanical) ─────────────────────────────────────────────────────────
def score(experiment, inject_at, duration):
    cfg = EXPERIMENTS[experiment]
    t0 = datetime.strptime(inject_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    window = duration + cfg["settle"]
    start = (t0 - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (t0 + timedelta(seconds=window)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Experiment: {experiment}   window: {inject_at} +{window}s")
    all_ok = True
    for c in cfg["checks"]:
        expr = c["expr"].format(ns=NS, w=window)
        if "increase(" in c["expr"]:
            # Cumulative-counter checks ("stay near zero over the window"). Evaluate
            # as ONE instant query at inject+window so increase([window]) looks back
            # exactly to inject — events from a PRIOR experiment (e.g. postgres 500s
            # leaking into the signer score) can't bleed in via the lookback.
            observed = scalar(query_instant(expr, end))
        else:
            # Gauge/quantile checks (dependency_up, p95) — range + aggregate, with
            # the -20s lead so the pre-inject baseline is visible.
            observed = aggregate(query_range(expr, start, end), c.get("agg", "max"))
        ok = _OPS[c["op"]](observed, c["threshold"])
        all_ok &= ok
        print(f"  [{'✓' if ok else '✗'}] {c['name']:<55} "
              f"observed={observed:.3g}{c['unit']}  ({c['op']} {c['threshold']})")
    print(f"  VERDICT: {'PASS' if all_ok else 'FAIL'}")
    return all_ok

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment", choices=list(EXPERIMENTS))
    ap.add_argument("--inject-at", required=True, help="UTC inject time, e.g. 2026-06-19T16:09:12Z")
    ap.add_argument("--duration", type=int, default=60, help="chaos duration seconds (default 60)")
    ap.add_argument("--prom", default=os.environ.get("PROM_URL", PROM),
                    help="Prometheus base URL (default: in-cluster svc, or $PROM_URL)")
    args = ap.parse_args()
    PROM = args.prom  # rebinds the module global query_range() reads
    sys.exit(0 if score(args.experiment, args.inject_at, args.duration) else 1)
