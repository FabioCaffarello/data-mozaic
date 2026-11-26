"""BC08 exact immutable contract resolution for trusted consumers."""

from .registry import (
    ContractReference,
    ContractResolutionError,
    ImmutableContractRegistry,
    ResolvedJSONSchema,
)

__all__ = [
    "ContractReference",
    "ContractResolutionError",
    "ImmutableContractRegistry",
    "ResolvedJSONSchema",
]
