"""Architecture diagrams as code (mingrammer/diagrams). Writes three PNGs next
to this file: 01-platform-architecture, 02-promotion-flow, 03-chaos-gate.

Run: docs/diagrams/.venv/bin/python docs/diagrams/architecture.py
Requires graphviz (`brew install graphviz`) and `pip install diagrams`.
Branded components use logos in icons/, with a k8s icon fallback if missing.
"""

import os

from diagrams import Diagram, Cluster, Edge
from diagrams.onprem.iac import Terraform
from diagrams.onprem.vcs import Github
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.gitops import ArgoCD
from diagrams.onprem.certificates import CertManager
from diagrams.onprem.monitoring import Prometheus, Grafana
from diagrams.onprem.logging import Loki
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.inmemory import Redis
from diagrams.onprem.client import Users
from diagrams.onprem.network import Internet
from diagrams.onprem.security import Vault
from diagrams.gcp.compute import KubernetesEngine
from diagrams.gcp.devtools import ContainerRegistry
from diagrams.gcp.security import KeyManagementService
from diagrams.gcp.storage import GCS
from diagrams.k8s.compute import Job, DS, Cronjob
from diagrams.k8s.others import CRD
from diagrams.k8s.rbac import SA
from diagrams.programming.framework import Fastapi
from diagrams.generic.blank import Blank
from diagrams.custom import Custom

BASE = os.path.dirname(os.path.abspath(__file__))
ICONS = os.path.join(BASE, "icons")

# Edge styles.
GATE = {"color": "firebrick", "fontcolor": "firebrick", "penwidth": "2.2"}
FAULT = {"color": "firebrick", "style": "dotted", "fontcolor": "firebrick"}
DASH = {"style": "dashed", "color": "gray45", "fontcolor": "gray35"}
OBS = {"color": "darkorange", "fontcolor": "darkorange"}
PAY = {"color": "#2e7d32", "fontcolor": "#2e7d32"}

GRAPH = {
    "fontsize": "22",
    "bgcolor": "white",
    "pad": "0.5",
    "nodesep": "0.5",
    "ranksep": "0.9",
    "splines": "spline",
}


def _custom(name, label, fallback):
    p = os.path.join(ICONS, name)
    return Custom(label, p) if os.path.exists(p) else fallback(label)


def kargo(label):
    return _custom("kargo.png", label, CRD)


def chaos(label):
    return _custom("chaos-mesh.png", label, CRD)


def eso(label):
    return _custom("eso.png", label, CRD)


def radius(label):
    # Falls back to a neutral external-service icon until icons/radius.png exists.
    return _custom("radius.png", label, Internet)


def cosign(label):
    return _custom("cosign.png", label, Blank)


# 01 — Platform architecture (GKE as the substrate)
GRAPH_MAIN = dict(GRAPH, ranksep="1.3", nodesep="0.6", fontsize="24")
with Diagram(
    "k8s-chaos-promotion — Platform Architecture",
    filename=os.path.join(BASE, "01-platform-architecture"),
    outformat="png",
    show=False,
    direction="TB",
    graph_attr=GRAPH_MAIN,
):
    # ---- Supply chain / GCP project + IaC (outside the cluster) ----
    dev = Users("developer")
    tf = Terraform("Terraform (IaC)\nGCS backend")
    rad = radius("Radius x402\nfacilitator (external)")

    with Cluster("GCP project: ajprojectplatform"):
        with Cluster("Source + CI  (keyless OIDC / WIF)"):
            gh = Github("repo\nmain + env/*")
            ci = GithubActions("Actions\nbuild")
            csign = cosign("Cosign\nkeyless sign")
            gar = ContainerRegistry("GAR\napp / signer / gate-runner")
            dev >> Edge(label="git push") >> gh >> Edge(label="app/signer/**") >> ci
            ci >> Edge(label="push :sha-") >> gar
            ci >> Edge(label="sign", **DASH) >> csign
        sm = KeyManagementService("Secret Manager")
        gcs = GCS("Terraform state")

    # ---- The GKE cluster: the platform substrate ----
    with Cluster(
        "GKE Standard — chaos-promotion   "
        "(us-central1-a · VPC-native · Workload Identity · default-pool 2x e2-standard-2)"
    ):
        gke = KubernetesEngine("cluster API /\ncontrol plane")

        with Cluster("Platform control plane  (operators, one per namespace)"):
            argo = ArgoCD("ArgoCD\nApp-of-Apps\nargocd")
            kg = kargo("Kargo\nkargo")
            cert = CertManager("cert-manager")
            esn = eso("External Secrets\nexternal-secrets")
            cmesh = chaos("Chaos Mesh 2.8.2\nchaos-mesh")
            with Cluster("monitoring"):
                prom = Prometheus("Prometheus")
                graf = Grafana("Grafana")
                loki = Loki("Loki")
                ptail = DS("Promtail")

        with Cluster("Tenant workloads  (Kargo-promoted -> ArgoCD-synced)"):
            with Cluster("url-shortener  (Kargo CRs + gate)"):
                wh = CRD("Warehouse")
                stages = CRD("3 Stages +\nApplicationSet")
                gate = Job("chaos-gate Job")
                gsa = SA("chaos-gate SA\n(cross-ns)")
            with Cluster("url-shortener-dev"):
                appd = Fastapi("app x1")
                pgd = PostgreSQL("postgres")
                rdd = Redis("redis")
            with Cluster("url-shortener-staging   (chaos-enabled)"):
                apps = Fastapi("app x2")
                pgs = PostgreSQL("postgres")
                rds = Redis("redis")
                sgn = Vault("radius-signer\n(Permit2 keys)")
                lg = Cronjob("k6 loadgen")
            with Cluster("url-shortener-prod"):
                appp = Fastapi("app x2")
                pgp = PostgreSQL("postgres")
                rdp = Redis("redis")

    # ---- IaC ----
    tf >> Edge(label="provisions", **DASH) >> gke
    tf >> Edge(**DASH) >> gar
    tf >> Edge(**DASH) >> gcs

    # ---- Supply -> promotion ----
    gar >> Edge(label="detect ^sha-") >> wh
    gh >> Edge(label="git sub: helm/**", **DASH) >> wh
    wh >> Edge(label="Freight") >> stages
    kg >> Edge(label="reconciles", **DASH) >> stages
    stages >> Edge(label="render env/* + sync", **DASH) >> argo
    argo >> Edge(label="applies") >> appd
    argo >> Edge() >> apps
    argo >> Edge() >> appp
    argo >> Edge(label="App-of-Apps (waves)", **DASH) >> cmesh

    # ---- Gate ----
    gsa >> Edge(**DASH) >> gate
    gate >> Edge(label="gates staging -> prod", **GATE) >> stages

    # ---- Secrets ----
    esn >> Edge(label="Workload Identity") >> sm
    esn >> Edge(label="sync secrets", **DASH) >> apps

    # ---- Chaos + observability + payment ----
    cmesh >> Edge(label="pod-failure", **FAULT) >> apps
    apps >> Edge(label="/metrics", **OBS) >> prom
    sgn >> Edge(**OBS) >> prom
    prom >> Edge(**OBS) >> graf
    ptail >> Edge(**OBS) >> loki
    lg >> Edge(label="sign", **PAY) >> sgn
    apps >> Edge(label="x402 verify / settle", **PAY) >> rad


# 02 — Promotion flow (git push -> prod, chaos gate highlighted)
with Diagram(
    "Chaos-Gated Promotion Flow",
    filename=os.path.join(BASE, "02-promotion-flow"),
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=GRAPH,
):
    dev = Users("developer\n(git push)")

    with Cluster("CI — GitHub Actions (keyless OIDC)"):
        ci = GithubActions("build")
        csign = cosign("Cosign sign")
    gar = ContainerRegistry("GAR\nsha-<commit>")

    with Cluster("Kargo — promotion engine"):
        kg = kargo("Warehouse")

    with Cluster("Promotion path (Kargo Stages)"):
        sdev = CRD("dev\nauto-promote")
        sstg = CRD("staging\nhealth + CHAOS GATE")
        sprod = CRD("prod\nmanual approve")
        sdev >> Edge(label="health-check x3") >> sstg
        sstg >> Edge(label="survives chaos", **GATE) >> sprod

    argo = ArgoCD("ArgoCD\napplies env/* YAML")

    with Cluster("GKE cluster"):
        with Cluster("url-shortener-dev"):
            envd = Fastapi("app")
        with Cluster("url-shortener-staging  (chaos-enabled)"):
            envs = Fastapi("app x2")
            cm = chaos("Chaos Mesh\nWorkflow")
        with Cluster("url-shortener-prod"):
            envp = Fastapi("app x2")

    dev >> ci >> Edge(label="image") >> gar >> Edge(label="detect ^sha-") >> kg
    ci >> Edge(**DASH) >> csign
    kg >> Edge(label="Freight") >> sdev
    sdev >> Edge(label="render env/dev", **DASH) >> argo
    sstg >> Edge(label="render env/staging", **DASH) >> argo
    sprod >> Edge(label="render env/prod", **DASH) >> argo
    argo >> Edge(label="sync") >> [envd, envs, envp]
    cm >> Edge(label="pod-failure", **FAULT) >> envs


# 03 — Inside the chaos gate (staging verification)
with Diagram(
    "Inside the Chaos Gate (staging)",
    filename=os.path.join(BASE, "03-chaos-gate"),
    outformat="png",
    show=False,
    direction="TB",
    graph_attr=GRAPH,
):
    with Cluster("Kargo  (ns: url-shortener)"):
        ar = CRD("AnalysisRun\nchaos-gate")
        gate = Job("gate Job\norchestrate.sh")
        gsa = SA("SA: chaos-gate")
        gsa >> Edge(**DASH) >> gate
        ar >> Edge(label="job provider") >> gate

    with Cluster("ns: url-shortener-staging"):
        lg = Cronjob("k6 loadgen\nreal x402 payments")
        with Cluster("Chaos Mesh Workflow (serial)"):
            cw = chaos("warmup -> postgres\n-> redis -> signer -> settle")
        app = Fastapi("url-shortener\napp x2")
        sgn = Vault("radius-signer\n(Permit2 keys)")
        pg = PostgreSQL("postgres")
        rd = Redis("redis")
        cw >> Edge(label="pod-failure", **FAULT) >> pg
        cw >> Edge(**FAULT) >> rd
        cw >> Edge(**FAULT) >> sgn
        lg >> Edge(label="sign", **PAY) >> sgn
        lg >> Edge(label="/shorten") >> app

    with Cluster("ns: monitoring"):
        prom = Prometheus("Prometheus")
        graf = Grafana("Grafana")

    rad = radius("Radius x402\nfacilitator")

    app >> Edge(label="verify / settle", **PAY) >> rad
    app >> Edge(label="/metrics", **OBS) >> prom

    gate >> Edge(label="1. fire loadgen") >> lg
    gate >> Edge(label="2. run Workflow") >> cw
    gate >> Edge(label="3. score (PromQL):\ntraffic floor / restarts /\ndependency_up / 5xx") >> prom
    gate >> Edge(label="exit 0 = PASS / 1 = FAIL", **GATE) >> ar
    gate >> Edge(label="4. annotate verdict", **DASH) >> graf

print("Wrote:")
for f in ("01-platform-architecture", "02-promotion-flow", "03-chaos-gate"):
    print("  ", os.path.join(BASE, f + ".png"))
