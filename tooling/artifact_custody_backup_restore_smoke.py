"""Logical BC06 PostgreSQL backup/restore into a temporary isolated schema."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit


def run(
    tool: str,
    args: list[str],
    env: dict[str, str],
    *,
    postgres_container: str,
    input_text: str | None = None,
) -> str:
    """Use the pinned Compose PostgreSQL image for version-matched client tools."""
    result = subprocess.run(
        ["docker", "exec", "-i", "-e", "PGPASSWORD", postgres_container, tool, *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1:] or ["no diagnostic output"]
        raise SystemExit(
            f"{tool} failed in the pinned PostgreSQL container: {detail[0]}"
        )
    return result.stdout


def main() -> None:
    if shutil.which("docker") is None:
        raise SystemExit("Docker is required for the isolated restore smoke")
    containers = subprocess.run(
        [
            "docker",
            "ps",
            "--quiet",
            "--filter=label=com.docker.compose.project=data-mozaic-scout-local",
            "--filter=label=com.docker.compose.service=postgres",
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    if len(containers) != 1:
        raise SystemExit(
            "the existing local PostgreSQL Compose service must be running exactly once"
        )
    postgres_container = containers[0]
    source = urlsplit(os.environ["ARTIFACT_CUSTODY_MIGRATOR_DATABASE_URL"])
    if (
        source.scheme not in ("postgres", "postgresql")
        or not source.username
        or not source.password
    ):
        raise SystemExit(
            "BC06 migrator DSN must use postgres://user:password@host/database form"
        )
    database = unquote(source.path.lstrip("/"))
    if not database or not re.fullmatch(r"[A-Za-z0-9_]+", database):
        raise SystemExit("database name is not safe for the restore smoke")
    schema = "bc06_restore_smoke_" + secrets.token_hex(6)
    env = os.environ.copy()
    env["PGPASSWORD"] = unquote(source.password)
    user = unquote(source.username)
    common = ["-h", "127.0.0.1", "-p", "5432", "-U", user, "-d", database]

    try:
        with tempfile.TemporaryDirectory(prefix="bc06-pg-backup-") as directory:
            backup = os.path.join(directory, "artifact-custody.sql")
            with open(backup, "w", encoding="utf-8") as stream:
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        "-e",
                        "PGPASSWORD",
                        postgres_container,
                        "pg_dump",
                        *common,
                        "--schema=artifact_custody",
                        "--no-owner",
                        "--no-acl",
                        "--format=plain",
                    ],
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    env=env,
                    text=True,
                    check=False,
                )
                if result.returncode:
                    detail = result.stderr.strip().splitlines()[-1:] or [
                        "no diagnostic output"
                    ]
                    raise SystemExit(f"pg_dump failed: {detail[0]}")
            with open(backup, encoding="utf-8") as stream:
                dump = stream.read()
            # pg_dump's generated schema identifier is replaced with an isolated
            # per-run namespace. Its name contains only fixed ASCII + hex.
            dump = re.sub(
                r"(?<![A-Za-z0-9_])artifact_custody(?![A-Za-z0-9_])", schema, dump
            )
            run(
                "psql",
                [*common, "--set=ON_ERROR_STOP=1", "--single-transaction"],
                env,
                postgres_container=postgres_container,
                input_text=dump,
            )

        def fingerprint(target_schema: str) -> str:
            if not re.fullmatch(r"[a-z0-9_]+", target_schema):
                raise ValueError("unsafe schema name")
            queries = [
                f"SELECT 'inventory',count(*),coalesce(md5(string_agg(tenant_id||E'\\x1f'||artifact_id||E'\\x1f'||intent_digest||E'\\x1f'||coalesce(receipt->>'receipt_id',''),E'\\n' ORDER BY tenant_id,artifact_id)),md5('')) FROM {target_schema}.artifact_inventory",
                f"SELECT 'operations',count(*),coalesce(md5(string_agg(tenant_id||E'\\x1f'||operation_id||E'\\x1f'||intent_digest||E'\\x1f'||coalesce(receipt->>'receipt_id',''),E'\\n' ORDER BY tenant_id,operation_id)),md5('')) FROM {target_schema}.custody_operations",
                f"SELECT 'outbox',count(*),coalesce(md5(string_agg(event_id||E'\\x1f'||event::text,E'\\n' ORDER BY event_id)),md5('')) FROM {target_schema}.custody_outbox",
                f"SELECT 'migrations',count(*),coalesce(md5(string_agg(version||E'\\x1f'||checksum,E'\\n' ORDER BY version)),md5('')) FROM {target_schema}.schema_migrations",
            ]
            return run(
                "psql",
                [
                    *common,
                    "--tuples-only",
                    "--no-align",
                    "--set=ON_ERROR_STOP=1",
                ],
                env,
                postgres_container=postgres_container,
                input_text=";\n".join(queries) + ";\n",
            ).strip()

        original, restored = fingerprint("artifact_custody"), fingerprint(schema)
        if original != restored:
            raise SystemExit(
                f"isolated BC06 PostgreSQL restore fingerprints differ (original={original!r}, restored={restored!r})"
            )
        print(
            "BC06 isolated PostgreSQL logical backup/restore passed; four table fingerprints matched. Object bytes are not included in this PostgreSQL-only smoke."
        )
    finally:
        # Remove only this run's random, isolated schema. No application schema
        # or developer data is touched.
        run(
            "psql",
            [*common, "--set=ON_ERROR_STOP=1"],
            env,
            postgres_container=postgres_container,
            input_text=f'DROP SCHEMA IF EXISTS "{schema}" CASCADE;\n',
        )


if __name__ == "__main__":
    main()
