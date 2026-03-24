resource "google_project_service" "iam_credentials" {
  project            = var.project_id
  service            = "iamcredentials.googleapis.com"
  disable_on_destroy = false
}

# STS is what actually exchanges the GitHub OIDC token for a short-lived GCP access token
resource "google_project_service" "sts" {
  project            = var.project_id
  service            = "sts.googleapis.com"
  disable_on_destroy = false
}

# Workload Identity Pool for GitHub Actions
resource "google_iam_workload_identity_pool" "github_pool" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions-pool"
  display_name              = "GitHub Actions Pool"
  description               = "Workload Identity Pool for GitHub Actions OIDC authentication"

  depends_on = [
    google_project_service.iam_credentials,
    google_project_service.sts,
  ]
}

# OIDC provider mapping GitHub tokens to GCP identities
resource "google_iam_workload_identity_pool_provider" "github_provider" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github_pool.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub OIDC Provider"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  # Map GitHub OIDC claims to GCP attributes
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
  }

  # Restrict to this specific repo — prevents other repos from using this pool
  attribute_condition = "attribute.repository == '${var.github_repo}'"
}

# Dedicated SA for GitHub Actions — only has write access to GAR
resource "google_service_account" "github_actions" {
  project      = var.project_id
  account_id   = "github-actions-sa"
  display_name = "GitHub Actions SA"
  description  = "Used by GitHub Actions CI to push images to GAR via OIDC (no static credentials)"
}

resource "google_project_iam_member" "github_actions_gar_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.github_actions.email}"
}

# Allow the GitHub OIDC token (from this repo) to impersonate the SA
resource "google_service_account_iam_member" "github_oidc_binding" {
  service_account_id = google_service_account.github_actions.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_pool.name}/attribute.repository/${var.github_repo}"
}

output "workload_identity_provider" {
  value       = google_iam_workload_identity_pool_provider.github_provider.name
  description = "Full WI provider resource name — use as workload_identity_provider in github-actions/auth@v2."
}

output "github_actions_sa" {
  value       = google_service_account.github_actions.email
  description = "GitHub Actions service account email — use as service_account in github-actions/auth@v2."
}
