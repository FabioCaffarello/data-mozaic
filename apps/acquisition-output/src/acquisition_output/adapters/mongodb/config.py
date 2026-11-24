"""Validated configuration for one MongoDB client or provisioning client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import MongoAdapterError


@dataclass(frozen=True, slots=True)
class MongoSettings:
    uri: str
    database: str = "bc05_outputs"
    max_pool_size: int = 32
    min_pool_size: int = 0
    server_selection_timeout_ms: int = 5_000
    connect_timeout_ms: int = 3_000
    socket_timeout_ms: int = 15_000
    operation_timeout_ms: int = 20_000
    transaction_commit_timeout_ms: int = 8_000

    def __post_init__(self) -> None:
        parsed = urlsplit(self.uri)
        if parsed.scheme not in {"mongodb", "mongodb+srv"} or not parsed.hostname:
            raise MongoAdapterError("MongoDB URI must use mongodb:// or mongodb+srv://")
        if not self.database or "/" in self.database or "\x00" in self.database:
            raise MongoAdapterError("MongoDB database name is invalid")
        if (
            not 1 <= self.max_pool_size <= 256
            or not 0 <= self.min_pool_size <= self.max_pool_size
        ):
            raise MongoAdapterError("MongoDB pool bounds are invalid")
        for value in (
            self.server_selection_timeout_ms,
            self.connect_timeout_ms,
            self.socket_timeout_ms,
            self.operation_timeout_ms,
            self.transaction_commit_timeout_ms,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise MongoAdapterError(
                    "MongoDB timeout values must be positive integers"
                )

    @classmethod
    def from_env(cls, prefix: str = "BC05_MONGODB") -> MongoSettings:
        uri = os.environ.get(f"{prefix}_URI", "")
        if not uri:
            raise MongoAdapterError(f"{prefix}_URI is required")
        try:
            return cls(
                uri=uri,
                database=os.environ.get(f"{prefix}_DATABASE", "bc05_outputs"),
                max_pool_size=int(os.environ.get(f"{prefix}_MAX_POOL_SIZE", "32")),
                min_pool_size=int(os.environ.get(f"{prefix}_MIN_POOL_SIZE", "0")),
                server_selection_timeout_ms=int(
                    os.environ.get(f"{prefix}_SERVER_SELECTION_TIMEOUT_MS", "5000")
                ),
                connect_timeout_ms=int(
                    os.environ.get(f"{prefix}_CONNECT_TIMEOUT_MS", "3000")
                ),
                socket_timeout_ms=int(
                    os.environ.get(f"{prefix}_SOCKET_TIMEOUT_MS", "15000")
                ),
                operation_timeout_ms=int(
                    os.environ.get(f"{prefix}_OPERATION_TIMEOUT_MS", "20000")
                ),
                transaction_commit_timeout_ms=int(
                    os.environ.get(f"{prefix}_TRANSACTION_COMMIT_TIMEOUT_MS", "8000")
                ),
            )
        except ValueError as error:
            raise MongoAdapterError(
                f"{prefix} numeric configuration is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class MongoProvisioningSettings:
    uri: str
    database: str = "bc05_outputs"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.uri)
        if parsed.scheme not in {"mongodb", "mongodb+srv"} or not parsed.hostname:
            raise MongoAdapterError(
                "MongoDB provisioning URI must use mongodb:// or mongodb+srv://"
            )
        if not self.database or "/" in self.database or "\x00" in self.database:
            raise MongoAdapterError("MongoDB provisioning database name is invalid")

    @classmethod
    def from_env(cls) -> MongoProvisioningSettings:
        uri = os.environ.get("BC05_MONGODB_ADMIN_URI", "")
        if not uri:
            raise MongoAdapterError(
                "BC05_MONGODB_ADMIN_URI is required for provisioning"
            )
        return cls(uri, os.environ.get("BC05_MONGODB_DATABASE", "bc05_outputs"))
