"""Shared closed YAML syntax primitives for BC15 draft DSL versions."""

from __future__ import annotations

import yaml


class PlaybookError(ValueError):
    """Input cannot safely produce, validate, or execute a playbook."""


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping_without_duplicate_keys(loader: UniqueKeyLoader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise PlaybookError("YAML mapping keys must be strings")
        if key in mapping:
            raise PlaybookError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _mapping_without_duplicate_keys,
)
