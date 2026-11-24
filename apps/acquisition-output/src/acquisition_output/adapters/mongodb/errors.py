"""Stable BC05 infrastructure error categories for MongoDB failures."""


class MongoAdapterError(RuntimeError):
    """Base persistence failure; driver messages are intentionally omitted."""


class MongoUnavailable(MongoAdapterError):
    """MongoDB could not satisfy an operation within configured bounds."""


class MongoProvisioningDrift(MongoAdapterError):
    """Existing collection validator or index differs from BC05 metadata."""
