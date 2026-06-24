#!/usr/bin/env python3
"""Mark a chaos-gate run on the Grafana dashboard timeline (verdict on the panels).

Posts a Grafana annotation (region: time→timeEnd over the run window) tagged
`chaos-gate` + `verdict:pass|fail`, so the "Chaos Verdict & Impact" dashboard
shows a green/red marker aligned with the App-Restarts / dependency_up panels —
turning Kargo's bare "Analysis failed" into a one-glance "why".

NON-FATAL BY DESIGN: the gate verdict must never depend on annotation success, so
any problem (no token, network, unparseable time) prints a note and exits 0.

Env: GRAFANA_URL (default in-cluster svc) + GRAFANA_TOKEN (a Grafana SA token;
if unset — e.g. a standalone debug run without the ESO secret — we just skip).
The image has no curl, so this uses stdlib urllib (same as score_experiment.py).
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone


def _to_ms(iso):
    """Workflow .status.startTime/endTime are 'YYYY-MM-DDTHH:MM:SSZ'. None on miss."""
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdict", required=True, choices=["pass", "fail"])
    ap.add_argument("--failed", default="", help="comma-sep experiment names that failed")
    ap.add_argument("--start", default="", help="run start, ISO8601 Z (workflow .status.startTime)")
    ap.add_argument("--end", default="", help="run end, ISO8601 Z (workflow .status.endTime)")
    ap.add_argument("--workflow", default="", help="workflow name, for the annotation text")
    a = ap.parse_args()

    url = os.environ.get("GRAFANA_URL", "http://observability-grafana.monitoring.svc").rstrip("/")
    token = os.environ.get("GRAFANA_TOKEN", "").strip()
    if not token:
        print(">> annotate: GRAFANA_TOKEN unset — skipping (annotation is optional)")
        return 0

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = _to_ms(a.start) or now_ms
    end = _to_ms(a.end) or now_ms
    if end <= start:                       # point/garbage window → give it 1s so it renders
        end = start + 1000

    text = f"chaos-gate {a.verdict.upper()}"
    if a.verdict == "fail" and a.failed:
        text += f" — failed: {a.failed}"
    if a.workflow:
        text += f" ({a.workflow})"

    body = json.dumps({
        "time": start,
        "timeEnd": end,
        "tags": ["chaos-gate", f"verdict:{a.verdict}"],
        "text": text,
    }).encode()
    req = urllib.request.Request(
        url + "/api/annotations", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f">> annotate: posted ({r.status}) — {text}")
    except Exception as e:                  # noqa: BLE001 — non-fatal on purpose
        print(f"!! annotate failed (non-fatal): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
