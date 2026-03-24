resource "google_project_service" "artifact_registry" {
  project            = var.project_id
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "app_repo" {
  location      = var.region
  repository_id = "k8s-chaos-demo"
  format        = "DOCKER"

  labels = local.common_labels

  depends_on = [google_project_service.artifact_registry]
}

output "gar_repository_url" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app_repo.repository_id}"
  description = "Base URL for the GAR Docker repository (append /<image>:<tag> to get full image path)."
}
