# --- SSH Key (use existing or create) ---

data "digitalocean_ssh_keys" "all" {}

locals {
  ssh_key_ids = var.ssh_key_fingerprint != "" ? [
    for k in data.digitalocean_ssh_keys.all.ssh_keys : k.id
    if k.fingerprint == var.ssh_key_fingerprint
  ] : [for k in data.digitalocean_ssh_keys.all.ssh_keys : k.id]
}

# --- Reserved IP (so we know the IP before droplet creation) ---

resource "digitalocean_reserved_ip" "app" {
  region = var.region
}

# --- Managed PostgreSQL ---

resource "digitalocean_database_cluster" "postgres" {
  name       = "donate-db"
  engine     = "pg"
  version    = "16"
  size       = "db-s-1vcpu-1gb"
  region     = var.region
  node_count = 1
}

resource "digitalocean_database_db" "donate" {
  cluster_id = digitalocean_database_cluster.postgres.id
  name       = "donate"
}

resource "digitalocean_database_user" "donate" {
  cluster_id = digitalocean_database_cluster.postgres.id
  name       = "donate"
}

# --- Droplet (Redis runs as Docker container on the droplet) ---

locals {
  site_url = var.domain != "" ? "https://${var.domain}" : "http://${digitalocean_reserved_ip.app.ip_address}"
  domain   = var.domain != "" ? var.domain : digitalocean_reserved_ip.app.ip_address
}

resource "digitalocean_droplet" "app" {
  name     = "donate-app"
  image    = "docker-20-04"
  size     = "s-2vcpu-2gb"
  region   = var.region
  ssh_keys = local.ssh_key_ids

  user_data = templatefile("${path.module}/cloud-init.yml", {
    database_url  = "postgresql+asyncpg://${digitalocean_database_user.donate.name}:${digitalocean_database_user.donate.password}@${digitalocean_database_cluster.postgres.private_host}:${digitalocean_database_cluster.postgres.port}/${digitalocean_database_db.donate.name}?ssl=require"
    redis_url     = "redis://redis:6379/0"
    anthropic_key = var.anthropic_api_key
    stripe_key    = var.stripe_key
    site_url      = local.site_url
    domain        = local.domain
  })

  depends_on = [
    digitalocean_database_cluster.postgres,
  ]
}

# --- Assign reserved IP to droplet ---

resource "digitalocean_reserved_ip_assignment" "app" {
  ip_address = digitalocean_reserved_ip.app.ip_address
  droplet_id = digitalocean_droplet.app.id
}

# --- Firewall ---

resource "digitalocean_firewall" "app" {
  name        = "donate-app-fw"
  droplet_ids = [digitalocean_droplet.app.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}

# --- Database Firewall (allow only droplet) ---

resource "digitalocean_database_firewall" "postgres" {
  cluster_id = digitalocean_database_cluster.postgres.id

  rule {
    type  = "droplet"
    value = digitalocean_droplet.app.id
  }
}

# --- DNS (optional, if domain is provided) ---

resource "digitalocean_domain" "app" {
  count = var.domain != "" ? 1 : 0
  name  = var.domain
}

resource "digitalocean_record" "app_a" {
  count  = var.domain != "" ? 1 : 0
  domain = digitalocean_domain.app[0].id
  type   = "A"
  name   = "@"
  value  = digitalocean_reserved_ip.app.ip_address
  ttl    = 300
}
