terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.22"
    }
  }
  backend "gcs" {
    bucket = "amoghdevops-tf-state"
    prefix = "gke/terraform.tfstate"
  }
}
provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

locals {
  network_name            = "${var.cluster_name}-vpc"
  subnetwork_name         = "${var.cluster_name}-subnet"
  cluster_secondary_range = "${var.cluster_name}-pods"
  svc_secondary_range     = "${var.cluster_name}-services"
  namespaces = [
    "url-shortener",
    "url-shortener-dev",
    "url-shortener-staging",
    "url-shortener-prod",
    "argocd",
    "kargo",
    "chaos-mesh",
    "monitoring",
  ]
  common_labels = {
    project    = "chaos-promotion"
    managed-by = "terraform"
  }

}

resource "google_project_service" "compute" {
  project            = var.project_id
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "container" {
  project            = var.project_id
  service            = "container.googleapis.com"
  disable_on_destroy = false
}

