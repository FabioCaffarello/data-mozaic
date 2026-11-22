"""Shared closed YAML syntax primitives for BC15 draft DSL versions."""

from __future__ import annotations

import json

import yaml


class PlaybookError(ValueError):
    """Input cannot safely produce, validate, or execute a playbook."""


def strict_json_loads(text: str | bytes, **kwargs):
    """Decode JSON while rejecting duplicate object keys."""

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    if "object_pairs_hook" in kwargs:
        raise TypeError("object_pairs_hook is controlled by strict_json_loads")
    return json.loads(text, object_pairs_hook=unique_object, **kwargs)


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
