variable "project_id" {
  description = "GCP Project ID"
  type        = string
  default     = "amoghdevops"
}

variable "region" {
  description = "GCP Region"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP Zone"
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  description = "GKE Cluster Name"
  type        = string
  default     = "chaos-promotion"
}

variable "default_node_count" {
  description = "Default number of nodes in the GKE cluster"
  type        = number
  default     = 2
}

variable "default_machine_type" {
  description = "Default machine type for GKE nodes"
  type        = string
  default     = "e2-standard-2"
}

variable "chaos_machine_type" {
  description = "Machine type for chaos nodes"
  type        = string
  default     = "e2-medium"
}

variable "github_repo" {
  description = "GitHub repository in owner/repo format — used to scope Workload Identity Federation to this repo only"
  type        = string
  default     = "amoghjay/k8s-chaos-promotion"
}
