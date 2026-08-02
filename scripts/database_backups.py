#!/usr/bin/env python3
"""Host-side PostgreSQL backup and restore helpers for Redoubt Matrix tenants."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


TENANT_POSTGRES_RE = re.compile(r"^(?P<tenant_id>\d+)_postgres$")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=True, **kwargs)


def capture(cmd: list[str]) -> str:
    result = run(cmd, stdout=subprocess.PIPE, text=True)
    return result.stdout


def resolve_output(output: Path | None, output_dir: Path, filename: str) -> Path:
    if output:
        return output
    return output_dir / filename


def ensure_backup_file(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"Backup file does not exist: {path}")


def retention_days(args: argparse.Namespace) -> int:
    if args.retention_days is not None:
        return args.retention_days
    return int(os.getenv("REDOUBT_BACKUP_RETENTION_DAYS", "0"))


def prune_old_backups(directory: Path, days: int) -> None:
    if days <= 0 or not directory.exists():
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for path in directory.glob("*.dump"):
        if not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            print(f"Pruned {path}")


def backup_container(container: str, user: str, database: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as backup:
        run(
            [
                "docker",
                "exec",
                container,
                "pg_dump",
                "-U",
                user,
                "-d",
                database,
                "--format=custom",
                "--compress=6",
                "--no-owner",
                "--no-acl",
            ],
            stdout=backup,
        )
    print(f"Wrote {output}")


def validate_container_archive(container: str, backup_path: Path) -> None:
    ensure_backup_file(backup_path)
    with backup_path.open("rb") as backup:
        run(
            ["docker", "exec", "-i", container, "pg_restore", "--list"],
            stdin=backup,
            stdout=subprocess.DEVNULL,
        )


def restore_container(
    container: str,
    user: str,
    database: str,
    backup_path: Path,
    *,
    validate: bool = True,
) -> None:
    if validate:
        validate_container_archive(container, backup_path)
    with backup_path.open("rb") as backup:
        run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "pg_restore",
                "-U",
                user,
                "-d",
                database,
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-acl",
                "--single-transaction",
                "--exit-on-error",
            ],
            stdin=backup,
        )


def running_container(container: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def stop_running(containers: list[str]) -> list[str]:
    running = [container for container in containers if running_container(container)]
    if running:
        run(["docker", "stop", *running])
    return running


def start_containers(containers: list[str]) -> None:
    if containers:
        run(["docker", "start", *containers])


def tenant_postgres_containers() -> list[tuple[str, str]]:
    names = capture(["docker", "ps", "--format", "{{.Names}}"]).splitlines()
    tenants: list[tuple[str, str]] = []
    for name in names:
        match = TENANT_POSTGRES_RE.match(name)
        if match:
            tenants.append((match.group("tenant_id"), name))
    return sorted(tenants, key=lambda value: int(value[0]))


def backup_tenant(args: argparse.Namespace) -> None:
    tenant_id = args.tenant_id
    output = resolve_output(
        args.output,
        args.output_dir,
        f"{tenant_id}-synapse-{utc_stamp()}.dump",
    )
    backup_container(f"{tenant_id}_postgres", "synapse", "synapse", output)
    prune_old_backups(output.parent, retention_days(args))


def backup_all_tenants(args: argparse.Namespace) -> None:
    tenants = tenant_postgres_containers()
    if not tenants:
        print("No running tenant Postgres containers found.")
        return

    failures: list[str] = []
    for tenant_id, container in tenants:
        output = args.output_dir / f"{tenant_id}-synapse-{utc_stamp()}.dump"
        try:
            backup_container(container, "synapse", "synapse", output)
        except subprocess.CalledProcessError:
            failures.append(tenant_id)

    prune_old_backups(args.output_dir, retention_days(args))
    if failures:
        raise SystemExit(f"Tenant backup failed for: {', '.join(failures)}")


def restore_tenant(args: argparse.Namespace) -> None:
    tenant_id = args.tenant_id
    backup_path = args.backup_path
    postgres_container = f"{tenant_id}_postgres"
    validate_container_archive(postgres_container, backup_path)

    stopped = stop_running([f"{tenant_id}_synapse", f"{tenant_id}_admin_portal"])
    try:
        restore_container(
            postgres_container,
            "synapse",
            "synapse",
            backup_path,
            validate=False,
        )
    finally:
        start_containers(stopped)


def add_backup_options(parser: argparse.ArgumentParser, *, output: bool = True) -> None:
    if output:
        parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("backups"))
    parser.add_argument("--retention-days", type=int)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(required=True)

    tenant_backup = subparsers.add_parser("backup-tenant")
    tenant_backup.add_argument("tenant_id")
    add_backup_options(tenant_backup)
    tenant_backup.set_defaults(func=backup_tenant)

    all_tenants_backup = subparsers.add_parser("backup-all-tenants")
    add_backup_options(all_tenants_backup, output=False)
    all_tenants_backup.set_defaults(func=backup_all_tenants)

    tenant_restore = subparsers.add_parser("restore-tenant")
    tenant_restore.add_argument("tenant_id")
    tenant_restore.add_argument("backup_path", type=Path)
    tenant_restore.set_defaults(func=restore_tenant)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
