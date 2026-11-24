"""Explicit owner of PyMongo client and database resource lifecycle."""

from __future__ import annotations

from typing import Self

from pymongo import MongoClient
from pymongo.database import Database

from .config import MongoSettings


class MongoResources:
    def __init__(self, settings: MongoSettings) -> None:
        self.settings = settings
        self.client: MongoClient | None = None

    def __enter__(self) -> Self:
        self.client = MongoClient(
            self.settings.uri,
            appname="data-mozaic-bc05",
            retryReads=True,
            retryWrites=True,
            maxPoolSize=self.settings.max_pool_size,
            minPoolSize=self.settings.min_pool_size,
            serverSelectionTimeoutMS=self.settings.server_selection_timeout_ms,
            connectTimeoutMS=self.settings.connect_timeout_ms,
            socketTimeoutMS=self.settings.socket_timeout_ms,
            timeoutMS=self.settings.operation_timeout_ms,
            tz_aware=True,
        )
        return self

    def __exit__(self, *_: object) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    @property
    def database(self) -> Database:
        if self.client is None:
            raise RuntimeError("MongoResources must be entered before use")
        return self.client[self.settings.database]
