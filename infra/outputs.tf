output "app_ip" {
  description = "Reserved IP address of the app"
  value       = digitalocean_reserved_ip.app.ip_address
}

output "app_url" {
  description = "URL to access the application"
  value       = local.site_url
}

output "postgres_host" {
  description = "PostgreSQL private hostname"
  value       = digitalocean_database_cluster.postgres.private_host
  sensitive   = true
}

output "redis_host" {
  description = "Redis private hostname"
  value       = digitalocean_database_cluster.redis.private_host
  sensitive   = true
}

output "ssh_command" {
  description = "SSH into the droplet"
  value       = "ssh root@${digitalocean_reserved_ip.app.ip_address}"
}

output "deploy_instructions" {
  description = "How to deploy the app"
  value       = "SSH in with: ssh root@${digitalocean_reserved_ip.app.ip_address} then run: /opt/donate/deploy.sh"
}
