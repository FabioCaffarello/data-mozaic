"""Minimal BC05 provisioning and configuration command line."""

from __future__ import annotations

import argparse

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from .client import MongoResources
from .config import MongoProvisioningSettings, MongoSettings
from .errors import MongoAdapterError
from .provision import provision


def main() -> int:
    parser = argparse.ArgumentParser(prog="bc05-mongodb")
    parser.add_argument("command", choices=("check-config", "provision", "ping"))
    args = parser.parse_args()
    try:
        if args.command == "check-config":
            app = MongoSettings.from_env()
            admin = MongoProvisioningSettings.from_env()
            if app.database != admin.database:
                raise MongoAdapterError(
                    "application and provisioning database names differ"
                )
            print("BC05 MongoDB configuration is valid (connection details redacted).")
            return 0
        if args.command == "provision":
            settings = MongoProvisioningSettings.from_env()
            with MongoClient(
                settings.uri,
                appname="data-mozaic-bc05-provisioner",
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=3000,
                timeoutMS=20000,
            ) as client:
                client.admin.command("ping")
                provision(client[settings.database])
            print("BC05 MongoDB schema version 2 is provisioned and drift-checked.")
            return 0
        settings = MongoSettings.from_env()
        with MongoResources(settings) as resources:
            assert resources.client is not None
            resources.client.admin.command("ping")
        print("BC05 MongoDB is reachable.")
        return 0
    except (MongoAdapterError, PyMongoError) as error:
        print(
            f"BC05 MongoDB command failed: {type(error).__name__}; connection details are redacted."
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
