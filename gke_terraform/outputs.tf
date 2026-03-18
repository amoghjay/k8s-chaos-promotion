output "cluster_name" {
  value       = google_container_cluster.gke_cluster.name
  description = "cluster_name is the name of the GKE cluster created by this Terraform configuration."

}

output "cluster_endpoint" {
  value       = google_container_cluster.gke_cluster.endpoint
  description = "cluster_endpoint is the endpoint of the GKE cluster created by this Terraform configuration."
}

output "cluster_location" {
  value       = google_container_cluster.gke_cluster.location
  description = "cluster_location is the location (zone or region) of the GKE cluster created by this Terraform configuration."
}

output "vpc_name" {
  value       = google_compute_network.gke_vpc.name
  description = "vpc_name is the name of the VPC network created for the GKE cluster."

}