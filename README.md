# Redoubt Matrix Deploy

Ansible automation for deploying isolated, per-tenant [Matrix](https://matrix.org) chat
infrastructure. Each tenant gets a dedicated [Synapse](https://github.com/element-hq/synapse)
homeserver, an [Element Web](https://github.com/element-hq/element-web) client, and a
Flask-based admin portal — all running in Docker on a single VPS, separated by network
and filesystem.

This is the infrastructure layer behind [Redoubt Systems](https://redoubt.systems), a
managed Matrix hosting service. The tooling is open-source; the operational orchestration
(billing, automated provisioning, multi-tenant management) is separate and private.

---

## Components

| Directory | Role |
|---|---|
| `ServerSetup/` | One-shot server hardening: UFW, Fail2Ban, Nginx + ModSecurity, Docker, msmtp |
| `MatrixDeploy/` | Per-tenant provisioning: Synapse, Element Web, admin portal, optional add-ons |
| `AdminDashboard/` | Flask web UI for tenant administrators — user and room management |

## Architecture

Each tenant is provisioned with:
- **Synapse** — Matrix homeserver, bound to `127.0.0.1`, proxied by Nginx
- **Element Web** — pre-built web client with per-tenant `config.json` branding
- **Admin portal** — Flask app wrapping the Synapse Admin API, served at `admin.{domain}`
- **Data volume** — sparse ext4 loop device, sized by plan, mounted at `/srv/matrix/{id}_chat/data/`

Tenants are identified by a numeric ID that forms the default subdomain
(`1001.chat.example.com`) and anchors all port assignments.

Optional add-ons:
- **Teleconferencing** — [LiveKit](https://livekit.io) SFU + lk-jwt-service for MatrixRTC group calls
- **Custom domain** — two-phase TLS flow; Synapse `server_name` is set correctly from day one

---

## Prerequisites

- A server running Ubuntu 22.04 LTS (fresh install) reachable via SSH as `root`
- Ansible 2.14+ with `community.docker` collection installed locally
- Docker CE will be installed by `ServerSetup/`; do not pre-install it

Install required Ansible collections:

```bash
ansible-galaxy collection install community.docker ansible.posix
```

---

## Quick Start

### 1. Run ServerSetup (once per server)

```bash
cd ServerSetup
cp inventory.yml inventory.develop.yml
# Edit inventory.develop.yml: fill in ansible_host, admin user details, SMTP config
cp roles/SourceFiles/VaultVars.yml roles/SourceFiles/vaultvars.develop.yml
# Edit vaultvars.develop.yml: fill in vault secrets
ansible-vault encrypt roles/SourceFiles/vaultvars.develop.yml
ansible-playbook site.yml -i inventory.develop.yml
```

### 2. Provision a Matrix tenant

```bash
cd MatrixDeploy
cp inventory.yml inventory.develop.yml
# Edit inventory.develop.yml: fill in server hostname and IP
cp roles/SourceFiles/VaultVars.yml roles/SourceFiles/vaultvars.develop.yml
# Edit vaultvars.develop.yml: fill in secrets
ansible-vault encrypt roles/SourceFiles/vaultvars.develop.yml

ansible-playbook addTenant.yml -i inventory.develop.yml --limit your-server-hostname \
  --extra-vars '{"id":"1001","plan_type":"starter","domain":"example","fulldomain":"1001.chat.example.com","matrix_port":3001,"element_port":3101,"admin_port":5001,"customer_email":"user@example.com"}'
```

### 3. Run AdminDashboard locally

```bash
cd AdminDashboard
cp .env.example .env
# Edit .env with your Synapse URL and credentials
pip install -r requirements.txt
sass scss/admin.scss static/css/admin.css  # requires sass
python app.py
```

---

## Plan Types

Plans are defined in `MatrixDeploy/inventory.yml`. Three tiers ship by default:

| Plan | Max Users | Memory | vCPU | Storage |
|---|---|---|---|---|
| `starter` | 15 | 450 MB | 0.25 | 5 GB |
| `community` | 75 | 900 MB | 0.5 | 40 GB |
| `organization` | 250 | 1800 MB | 1.0 | 75 GB |

Edit the inventory to adjust limits for your deployment.

---

## Playbooks

| Playbook | Purpose |
|---|---|
| `addTenant.yml` | Provision or re-provision a tenant (idempotent) |
| `disableTenant.yml` | Stop services; preserve data |
| `removeTenant.yml` | Tear down everything — **destructive** |
| `updateTenant.yml` | Apply plan or config changes |
| `flagPaymentFailed.yml` | Display payment-failed notice in the client |

---

## Security

See [SECURITY.md](SECURITY.md) for the threat model, secret management guidance, and
how to report vulnerabilities.

Key practices used in this project:
- All secrets via Ansible Vault — no credentials committed
- Synapse and admin portal bound to `localhost` only
- Admin portal: CSRF protection, rate-limited login, HSTS, security headers
- `no_log: true` on all Ansible tasks that handle secrets

---

## Contributing

Pull requests are welcome. Please open an issue first for significant changes.

This repo tracks the infrastructure layer only. The billing and multi-tenant orchestration
layer is not included here.

---

## License

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0)

Copyright (C) 2026 Redoubt Systems
