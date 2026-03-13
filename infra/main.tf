# --- SSH Key ---

data "digitalocean_ssh_keys" "all" {}

locals {
  ssh_key_ids = var.ssh_key_fingerprint != "" ? [
    for k in data.digitalocean_ssh_keys.all.ssh_keys : k.id
    if k.fingerprint == var.ssh_key_fingerprint
  ] : [for k in data.digitalocean_ssh_keys.all.ssh_keys : k.id]
}

# --- Reserved IP ---

resource "digitalocean_reserved_ip" "app" {
  region = var.region
}

# --- Droplet ---

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
    anthropic_key = var.anthropic_api_key
    stripe_key    = var.stripe_key
    site_url      = local.site_url
    domain        = local.domain
  })

  lifecycle {
    ignore_changes = [user_data]
  }
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

# --- DNS (optional) ---

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
