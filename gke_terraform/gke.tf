resource "google_container_cluster" "gke_cluster" {
  name     = var.cluster_name
  location = var.zone

  network    = google_compute_network.gke_vpc.self_link
  subnetwork = google_compute_subnetwork.gke_subnet.self_link

  remove_default_node_pool = true
  initial_node_count       = 1

  deletion_protection = false

  release_channel {
    channel = "REGULAR"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = local.cluster_secondary_range
    services_secondary_range_name = local.svc_secondary_range
  }

  networking_mode = "VPC_NATIVE"

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  resource_labels = local.common_labels

  depends_on = [
    google_project_service.compute,
    google_project_service.container,
  ]
}

resource "google_container_node_pool" "default" {
  name       = "default-pool"
  location   = var.zone
  cluster    = google_container_cluster.gke_cluster.name
  node_count = var.default_node_count

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type = var.default_machine_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    labels = {
      role = "default"
    }

    resource_labels = local.common_labels
  }
}

resource "google_container_node_pool" "chaos" {
  name     = "chaos-pool"
  location = var.zone
  cluster  = google_container_cluster.gke_cluster.name

  autoscaling {
    min_node_count = 0
    max_node_count = 1
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type = var.chaos_machine_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    spot         = true

    labels = {
      role = "chaos"
    }

    resource_labels = local.common_labels

    taint {
      key    = "role"
      value  = "chaos"
      effect = "NO_SCHEDULE"
    }
  }
}
