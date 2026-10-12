"""Checks a tool's arguments against its JSON Schema.

The tools' input schemas are plain JSON Schema (that is what MCP clients and chat APIs are
given). Only the keywords those schemas use are implemented here, so the farm needs no
schema library to validate what a model sends: type, enum, minimum, maximum, minLength,
maxLength, pattern, items, minItems, maxItems, properties, required, additionalProperties.
A unit test checks this against a full validator and fails on a keyword that is not handled.
"""
from __future__ import annotations

import re
from typing import Any

KEYWORDS = frozenset({"type", "enum", "minimum", "maximum", "minLength", "maxLength", "pattern", "items", "minItems",
                      "maxItems", "properties", "required", "additionalProperties", "description", "default", "title"})
_TYPES: dict[str, tuple[type, ...]] = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
                                        "array": (list,), "object": (dict,)}


def check(value: Any, schema: dict, where: str = "") -> None:
    """Raise ValueError naming the argument and the rule it breaks. The value is never
    echoed in the message: it may be text a model was told to type."""
    name = where or "arguments"
    kind = schema.get("type")
    if kind is not None:
        if not isinstance(value, _TYPES[kind]) or (kind != "boolean" and isinstance(value, bool)):
            raise ValueError(f"{name}: expected {_article(kind)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{name}: must be one of {', '.join(str(v) for v in schema['enum'])}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{name}: must be a finite number")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{name}: must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{name}: must be at most {schema['maximum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"{name}: must not be empty" if schema["minLength"] == 1
                             else f"{name}: must have at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{name}: must have at most {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValueError(f"{name}: does not have the expected form")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{name}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{name}: takes at most {schema['maxItems']} items")
        if "items" in schema:
            for n, item in enumerate(value):
                check(item, schema["items"], f"{name}[{n}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = [k for k in schema.get("required", []) if k not in value]
        if missing:
            raise ValueError(f"{name}: missing {', '.join(missing)}" if where else f"missing argument(s): {', '.join(missing)}")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            path = f"{where}.{key}" if where else str(key)[:60]
            if key in properties:
                check(item, properties[key], path)
            elif extra is False:
                known = ", ".join(sorted(properties)) or "none"
                raise ValueError(f"unknown argument {str(key)[:60]!r} (this tool takes: {known})" if not where
                                 else f"{where}: unknown field {str(key)[:60]!r}")
            elif isinstance(extra, dict):
                check(item, extra, path)


def _article(kind: str) -> str:
    return {"string": "text", "integer": "a whole number", "number": "a number", "boolean": "true or false",
            "array": "a list", "object": "an object"}[kind]


def unsupported_keywords(schema: Any) -> set[str]:
    """Keywords used in a schema that check() would silently ignore. Empty for every tool."""
    found: set[str] = set()
    if isinstance(schema, dict):
        found |= set(schema) - KEYWORDS
        for key in ("items", "additionalProperties"):
            if isinstance(schema.get(key), dict):
                found |= unsupported_keywords(schema[key])
        for sub in (schema.get("properties") or {}).values():
            found |= unsupported_keywords(sub)
    return found
