# Dedicated node SA — replaces the default Compute Engine SA on GKE nodes.
# Principle of least privilege: nodes only get what they actually need.
resource "google_service_account" "gke_nodes" {
  project      = var.project_id
  account_id   = "gke-node-sa"
  display_name = "GKE Node SA"
  description  = "Least-privilege SA for GKE nodes (logging, monitoring, GAR pull, Workload Identity metadata)"
}

# Send container/node logs to Cloud Logging
resource "google_project_iam_member" "gke_nodes_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Write node/pod metrics to Cloud Monitoring
resource "google_project_iam_member" "gke_nodes_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Required for kube-state-metrics and node-exporter to read monitoring data
resource "google_project_iam_member" "gke_nodes_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Pull images from GAR — needed since we moved off Docker Hub
resource "google_project_iam_member" "gke_nodes_gar_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

output "gke_node_sa_email" {
  value       = google_service_account.gke_nodes.email
  description = "GKE node service account email."
}
