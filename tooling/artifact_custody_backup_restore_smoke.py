"""Logical BC06 PostgreSQL backup/restore into a temporary isolated schema."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit


def run(args: list[str], env: dict[str, str], *, input_text: str | None = None) -> str:
    result = subprocess.run(
        args, input=input_text, text=True, capture_output=True, env=env, check=True
    )
    return result.stdout


def main() -> None:
    for command in ("pg_dump", "psql"):
        if shutil.which(command) is None:
            raise SystemExit(f"{command} is required for the isolated restore smoke")
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
    host = "127.0.0.1"
    port = os.environ.get("BC06_LOCAL_POSTGRES_PORT", "55432")
    user = unquote(source.username)
    common = ["-h", host, "-p", port, "-U", user, "-d", database]

    try:
        with tempfile.TemporaryDirectory(prefix="bc06-pg-backup-") as directory:
            backup = os.path.join(directory, "artifact-custody.sql")
            with open(backup, "w", encoding="utf-8") as stream:
                subprocess.run(
                    [
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
                    check=True,
                    text=True,
                )
            with open(backup, encoding="utf-8") as stream:
                dump = stream.read()
            # pg_dump's generated schema identifier is replaced with an isolated
            # per-run namespace. Its name contains only fixed ASCII + hex.
            dump = re.sub(
                r"(?<![A-Za-z0-9_])artifact_custody(?![A-Za-z0-9_])", schema, dump
            )
            run(
                ["psql", *common, "--set=ON_ERROR_STOP=1", "--single-transaction"],
                env,
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
                [
                    "psql",
                    *common,
                    "--tuples-only",
                    "--no-align",
                    "--set=ON_ERROR_STOP=1",
                ],
                env,
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
            ["psql", *common, "--set=ON_ERROR_STOP=1"],
            env,
            input_text=f'DROP SCHEMA IF EXISTS "{schema}" CASCADE;\n',
        )


if __name__ == "__main__":
    main()
