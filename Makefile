.PHONY: help bootstrap migrate verify dry-run config plan-only \
        phase-00 phase-01 phase-02 phase-03 phase-04 phase-05 phase-06 phase-07 \
        portforward portforward-stop scale-up scale-down

SCRIPTS := ./platform_setup_scripts/bootstrap.sh

help:
	@echo "k8s-chaos-promotion — Makefile targets"
	@echo ""
	@echo "  Setup:"
	@echo "    make config             Copy config.env.example to config.env if missing"
	@echo "    make bootstrap          Fresh setup — runs all phases interactively"
	@echo "    make migrate SOURCE=<project> [ACCOUNT=<email>]"
	@echo "                            Migration mode — copies secrets from SOURCE project"
	@echo "    make dry-run            Print what bootstrap would do, no changes"
	@echo "    make plan-only          Run through phase 03 with PLAN_ONLY (terraform plan, no apply)"
	@echo "    make verify             Run health checks (Phase 07 only)"
	@echo ""
	@echo "  Individual phases:"
	@echo "    make phase-00           Preflight checks"
	@echo "    make phase-01           Enable GCP APIs + create TF state bucket"
	@echo "    make phase-02           Create or migrate secrets"
	@echo "    make phase-03           Terraform (with safety prompt before apply)"
	@echo "    make phase-04           Helm installs (cert-manager, ArgoCD, ESO, Kargo, etc.)"
	@echo "    make phase-05           ClusterSecretStore + ArgoCD repo ExternalSecret"
	@echo "    make phase-06           root-app + Kargo CRs + ApplicationSet"
	@echo "    make phase-07           Verify only (no changes)"
	@echo ""
	@echo "  Operations:"
	@echo "    make portforward        Background all UI port-forwards (ArgoCD, Kargo, Grafana, Prom, Loki)"
	@echo "    make portforward-stop   Kill all kubectl port-forward processes"
	@echo "    make scale-up           Scale cluster nodes to 2 (start of session)"
	@echo "    make scale-down         Scale cluster nodes to 0 (end of session — saves cost)"

config:
	@[ -f platform_setup_scripts/config.env ] && echo "config.env already exists" || \
		(cp platform_setup_scripts/config.env.example platform_setup_scripts/config.env && \
		 echo "Created platform_setup_scripts/config.env — edit it before running bootstrap")

bootstrap:
	$(SCRIPTS)

migrate:
	@[ -n "$(SOURCE)" ] || (echo "Usage: make migrate SOURCE=<project-id> [ACCOUNT=<email>]"; exit 1)
	$(SCRIPTS) --source-project $(SOURCE) $(if $(ACCOUNT),--source-account $(ACCOUNT),)

dry-run:
	$(SCRIPTS) --dry-run

plan-only:
	PLAN_ONLY=true $(SCRIPTS) --to 3

verify:
	$(SCRIPTS) --phase 7

phase-00: ; $(SCRIPTS) --phase 0
phase-01: ; $(SCRIPTS) --phase 1
phase-02: ; $(SCRIPTS) --phase 2
phase-03: ; $(SCRIPTS) --phase 3
phase-04: ; $(SCRIPTS) --phase 4
phase-05: ; $(SCRIPTS) --phase 5
phase-06: ; $(SCRIPTS) --phase 6
phase-07: ; $(SCRIPTS) --phase 7

# --- Operational helpers ---

portforward:
	@kubectl port-forward svc/argocd-server -n argocd 8080:443 >/dev/null 2>&1 &
	@kubectl port-forward svc/kargo-api -n kargo 8081:443 >/dev/null 2>&1 &
	@kubectl port-forward svc/observability-grafana -n monitoring 3000:80 >/dev/null 2>&1 &
	@kubectl port-forward svc/observability-kube-prometh-prometheus -n monitoring 9090:9090 >/dev/null 2>&1 &
	@kubectl port-forward svc/observability-loki-gateway -n monitoring 3100:80 >/dev/null 2>&1 &
	@echo "✓ Port-forwards backgrounded:"
	@echo "    https://localhost:8080  ArgoCD"
	@echo "    https://localhost:8081  Kargo"
	@echo "    http://localhost:3000   Grafana"
	@echo "    http://localhost:9090   Prometheus"
	@echo "    http://localhost:3100   Loki"
	@echo ""
	@echo "Kill all: make portforward-stop"

portforward-stop:
	@pkill -f "kubectl port-forward" || true
	@echo "✓ All kubectl port-forwards stopped"

scale-up:
	@source platform_setup_scripts/config.env && \
	  gcloud container clusters resize $$CLUSTER_NAME --num-nodes=2 \
	    --node-pool=default-pool --zone=$$ZONE --project=$$PROJECT_ID -q

scale-down:
	@source platform_setup_scripts/config.env && \
	  gcloud container clusters resize $$CLUSTER_NAME --num-nodes=0 \
	    --node-pool=default-pool --zone=$$ZONE --project=$$PROJECT_ID -q
