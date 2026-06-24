# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities by email to **security@redoubt.systems**.
Do not open a public GitHub issue for security concerns.

You can expect an acknowledgment within 2 business days and a resolution timeline
within 5 business days for confirmed issues.

## Secret Management

All secrets in this project are managed via **Ansible Vault**.

- `VaultVars.yml` — encrypted vault file (committed). Contains placeholder structure only.
  Copy to `vaultvars.develop.yml` (gitignored), fill in real values, and encrypt with
  `ansible-vault encrypt vaultvars.develop.yml` before use.
- `inventory.develop.yml` — gitignored. Contains real hostnames, IPs, and SMTP config.
  Copy from `inventory.yml` and fill in real values. **Never commit this file.**
- `AdminDashboard/.env` — gitignored. Copy from `.env.example` and fill in real values.
  **Never commit this file.**
- `MatrixDeploy/roles/SourceFiles/AdminPortal/.env` — this file uses Ansible Jinja2
  template variables (`{{ smtp_password }}` etc.) and contains no real credentials.
  It is rendered by Ansible at deploy time using values from the vault.

## What to Keep Private

If you deploy this yourself, the following must never be committed or exposed:

- Vault passwords and vault-encrypted variable values
- `inventory.develop.yml` / `vaultvars.develop.yml`
- Any `.env` file containing real credentials
- SSH private keys
- Stripe API keys or webhook secrets (if running your own billing layer)
- The Ansible vault password file

## Threat Model

This project deploys per-tenant, isolated Matrix stacks. Each tenant's data is
separated by Docker networking, filesystem paths, and distinct service ports.
Synapse and the admin portal listen on localhost only; all external access is
proxied through Nginx with TLS termination.

The main attack surfaces are:
- Nginx (public-facing; kept patched via unattended-upgrades)
- Certbot / Let's Encrypt certificate renewal
- SSH access to the host (restricted to key auth, Fail2Ban active)
- The admin portal login (rate-limited, CSRF-protected)
