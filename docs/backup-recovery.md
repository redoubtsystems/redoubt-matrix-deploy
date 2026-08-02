# Database Backup and Recovery

Redoubt Matrix Deploy has two PostgreSQL backup paths:

- Tenant admins can download a password-encrypted homeserver database export from the Admin Dashboard.
- Operators can run host-side backup and restore tooling for tenant databases.

Operator backups are PostgreSQL custom-format dumps made with `pg_dump --format=custom`.
Admin Dashboard downloads package `manifest.json` and `synapse.dump` together, wrap the package in password-based GPG symmetric encryption, and use the `.tar.gpg` extension.

## Product Policy

Tenant admins can download password-encrypted database exports, but they cannot upload or restore databases from the Admin Dashboard.

Restore stays operator-managed because Matrix homeserver identity is not portable in the way a normal app database is. A Synapse database contains Matrix IDs and room IDs tied to the original `server_name`, such as `1003.chat.example.com`. Restoring that dump into a homeserver initialized as `1004.chat.example.com` would leave users and rooms bound to the old identity. Synapse schema compatibility also depends on the version that created the dump and the version that starts after restore.

For managed restores:

- Restore to the same Matrix `server_name`.
- Prefer restoring to the same Synapse version that made the backup, then let normal upgrades run.
- Do not treat a tenant database dump as a migration path to a different homeserver URL.

## Automated Tenant Backups

MatrixDeploy installs on tenant hosts:

- Script: `/opt/redoubt/scripts/database_backups.py`
- Timer: `redoubt-tenant-database-backup.timer`
- Output: `/srv/matrix/backups`
- Schedule: daily at 03:40 with up to 45 minutes randomized delay
- Retention: 14 days by default

Check timer state:

```bash
sudo systemctl list-timers 'redoubt*backup*'
```

Run a backup immediately:

```bash
sudo systemctl start redoubt-tenant-database-backup.service
```

Inspect logs:

```bash
sudo journalctl -u redoubt-tenant-database-backup.service
```

## Manual Operator Commands

Back up one tenant database:

```bash
sudo /opt/redoubt/scripts/database_backups.py backup-tenant 1001 --output-dir /srv/matrix/backups
```

Back up all running tenant databases on a tenant host:

```bash
sudo /opt/redoubt/scripts/database_backups.py backup-all-tenants --output-dir /srv/matrix/backups
```

Restore one tenant database:

```bash
sudo /opt/redoubt/scripts/database_backups.py restore-tenant 1001 /srv/matrix/backups/1001-synapse-20260801T034000Z.dump
```

Tenant restore validates the dump, stops `<tenant>_synapse` and `<tenant>_admin_portal`, restores into `<tenant>_postgres`, then starts the stopped containers again.

## Admin Dashboard Export

Tenant admins use `Server -> Database Export`, enter an export password, then download a `.tar.gpg` file.

Decrypt and unpack an Admin Dashboard export before operator restore:

```bash
mkdir restore-export
gpg --decrypt 1003.chat.example.com-synapse-export-20260801T034000Z.tar.gpg | tar -x -C restore-export
cat restore-export/manifest.json
```

The Admin Portal container needs:

- PostgreSQL 16 client tools in the image.
- GnuPG in the image.
- `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD`.

MatrixDeploy injects these values into each generated `admin-portal/.env`.

## Current Scope

These tools back up PostgreSQL databases. A complete tenant disaster recovery bundle also needs Synapse media files and identity/config state, including `/srv/matrix/<tenant>_chat/data/media_store`, the Synapse signing key, and relevant files in `/srv/matrix/<tenant>_chat/config`. Keep separate filesystem or off-host backups for those until a full tenant bundle exporter is implemented.

## Future Media Bundle

A full tenant export could extend the encrypted package with a compressed archive of `media_store` and manifest fields for media byte count, media checksum, and archive name.

Benefits:

- Preserves uploads, avatars, thumbnails, and encrypted attachment blobs that are not in PostgreSQL.
- Makes operator recovery less dependent on separate filesystem backups.
- Gives clients a clearer "all data we can export" artifact.

Challenges:

- Media archives can be large and slow enough to exceed HTTP request, proxy, or browser download limits.
- The export must avoid staging a second full copy of tenant media on the same quota-limited disk; it should stream compression and encryption.
- Consistency is harder than a DB dump alone because the database can reference media while Synapse is still writing files.
- Some media may be plaintext, such as avatars or files from unencrypted rooms, so the media archive must be encrypted too.
- Restoring media still has the same Matrix identity constraint as the database: it belongs with the original `server_name`, not a newly initialized homeserver URL.
