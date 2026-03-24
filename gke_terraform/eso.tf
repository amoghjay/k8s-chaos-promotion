resource "google_project_service" "secret_manager" {
  project            = var.project_id
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

# GCP SA that ESO uses to read secrets from Secret Manager
resource "google_service_account" "external_secrets" {
  project      = var.project_id
  account_id   = "external-secrets-sa"
  display_name = "External Secrets Operator SA"
  description  = "Used by ESO to read secrets from GCP Secret Manager via Workload Identity (no JSON key)"
}

resource "google_project_iam_member" "external_secrets_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.external_secrets.email}"

  depends_on = [google_project_service.secret_manager]
}

# Allow the ESO K8s SA (external-secrets/external-secrets) to impersonate the GCP SA
# This is the Workload Identity binding — no JSON key ever needed
resource "google_service_account_iam_member" "eso_workload_identity_binding" {
  service_account_id = google_service_account.external_secrets.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[external-secrets/external-secrets]"
}

output "eso_gcp_sa_email" {
  value       = google_service_account.external_secrets.email
  description = "ESO GCP SA email — annotate the K8s SA with iam.gke.io/gcp-service-account=<this value>."
}
