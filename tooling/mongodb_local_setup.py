"""Generate ignored credentials and the replica-set keyfile for local Compose."""

from __future__ import annotations

import base64
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.mongodb"
KEY_DIR = ROOT / ".local/mongodb"
KEY_FILE = KEY_DIR / "keyfile"


def main() -> int:
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    if not ENV.exists():
        root_user = "local_admin"
        app_user = "bc05_app"
        root_password = secrets.token_urlsafe(36)
        app_password = secrets.token_urlsafe(36)
        ENV.write_text(
            "\n".join(
                (
                    f"MONGO_ROOT_USER={root_user}",
                    f"MONGO_ROOT_PASSWORD={root_password}",
                    f"MONGO_APP_USER={app_user}",
                    f"MONGO_APP_PASSWORD={app_password}",
                    "BC05_MONGODB_DATABASE=bc05_outputs",
                    f"BC05_MONGODB_URI=mongodb://{app_user}:{app_password}@127.0.0.1:27017/bc05_outputs?authSource=bc05_outputs&replicaSet=rs0&directConnection=true",
                    f"BC05_MONGODB_ADMIN_URI=mongodb://{root_user}:{root_password}@127.0.0.1:27017/admin?authSource=admin&replicaSet=rs0&directConnection=true",
                    "",
                )
            ),
            encoding="utf-8",
        )
        ENV.chmod(0o600)
    # Keep the generated dotenv valid both for Compose and shell `source` in
    # the operator targets; MongoDB URIs contain shell metacharacters (`&`).
    lines = ENV.read_text(encoding="utf-8").splitlines()
    safe_lines = []
    for line in lines:
        if line.startswith(("BC05_MONGODB_URI=", "BC05_MONGODB_ADMIN_URI=")):
            key, value = line.split("=", 1)
            value = value.strip("'\"")
            line = f"{key}='{value}'"
        safe_lines.append(line)
    ENV.write_text("\n".join([*safe_lines, ""]), encoding="utf-8")
    ENV.chmod(0o600)
    key_is_valid = False
    if KEY_FILE.exists():
        try:
            key_is_valid = (
                len(base64.b64decode(KEY_FILE.read_bytes(), validate=True)) >= 6
            )
        except (ValueError, OSError):
            key_is_valid = False
    if not key_is_valid:
        if KEY_FILE.exists():
            KEY_FILE.chmod(0o600)
        KEY_FILE.write_bytes(base64.b64encode(secrets.token_bytes(768)))
        KEY_FILE.chmod(0o400)
    print(
        "Local MongoDB credentials and replica-set keyfile are ready (values hidden)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
