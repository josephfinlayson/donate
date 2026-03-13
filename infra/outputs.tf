output "app_ip" {
  description = "Reserved IP address of the app"
  value       = digitalocean_reserved_ip.app.ip_address
}

output "app_url" {
  description = "URL to access the application"
  value       = local.site_url
}

output "ssh_command" {
  description = "SSH into the droplet"
  value       = "ssh root@${digitalocean_reserved_ip.app.ip_address}"
}

output "deploy_instructions" {
  description = "How to deploy the app"
  value       = "Run: ./deploy.sh ${digitalocean_reserved_ip.app.ip_address}"
}
