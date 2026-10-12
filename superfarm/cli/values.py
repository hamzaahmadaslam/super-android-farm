"""`key=value` on the command line, typed by the model the key belongs to.

`sf set 3 fps=30 name=13` and `sf config set android.default_version 12` both hand us
text. Guessing the type from the text gets `name=13` and version `12` wrong (they are
strings), so the type is read from the pydantic model the key points into, and the final
word on validity stays with the model's own validation.

    network.mode=vpn            a field of a nested model
    tags=eu,test                a list: comma separated ("" or [] empties it)
    extra_props.ro.foo=bar      a dict entry: everything after the field name is the key
    extra_props=                a whole dict emptied ("" or {}), sent as null: a patch merges
                                dicts entry by entry, so an empty one would change nothing
    gpu_mode=default            an optional field back to "follow the farm default"
"""
from __future__ import annotations

import types
from typing import Any, Literal, Union, get_args, get_origin

import yaml
from pydantic import BaseModel

# The one word that means "no value of its own". Not `none`: that is a real network mode and a
# real device profile, and must never be read as "unset".
UNSET_WORD = "default"
_TRUE, _FALSE = ("true", "yes", "on", "1"), ("false", "no", "off", "0")


class KeyValueError(ValueError):
    """A bad key or value, with a message meant for the terminal."""


def _strip_optional(annotation: Any) -> tuple[Any, bool]:
    if get_origin(annotation) in (Union, types.UnionType):
        inner = [a for a in get_args(annotation) if a is not type(None)]
        if len(inner) == 1 and len(get_args(annotation)) == 2:
            return inner[0], True
    return annotation, False


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def keys_of(model: type[BaseModel], prefix: str = "") -> list[str]:
    """Every settable dotted key of a model, for error messages and completion."""
    out: list[str] = []
    for name, field in model.model_fields.items():
        annotation, _ = _strip_optional(field.annotation)
        if _is_model(annotation):
            out += keys_of(annotation, f"{prefix}{name}.")
        elif get_origin(annotation) is dict:
            out.append(f"{prefix}{name}.<name>")
        else:
            out.append(f"{prefix}{name}")
    return out


def locate(model: type[BaseModel], dotted: str) -> tuple[list[str], Any, bool]:
    """Find a dotted key in a model tree.

    Returns (path, annotation, optional): the path as nested-dict keys (a dict key that
    itself contains dots stays one element), the leaf's type, and whether None is allowed."""
    parts = dotted.split(".")
    if not dotted or any(not p for p in parts):
        raise KeyValueError(f"bad key {dotted!r}")
    path: list[str] = []
    current: Any = model
    optional = False
    i = 0
    while i < len(parts):
        if _is_model(current):
            field = current.model_fields.get(parts[i])
            if field is None:
                where = ".".join(path) or "this object"
                known = ", ".join(sorted(current.model_fields))
                raise KeyValueError(f"unknown key {dotted!r}: {where} has no field {parts[i]!r} (fields: {known})")
            path.append(parts[i])
            current, optional = _strip_optional(field.annotation)
            i += 1
        elif get_origin(current) is dict:
            value_type = get_args(current)[1]
            if _is_model(value_type):
                path.append(parts[i])
                i += 1
            else:
                # Property names contain dots (ro.product.model): the rest is one key.
                path.append(".".join(parts[i:]))
                i = len(parts)
            current, optional = value_type, False
        else:
            raise KeyValueError(f"unknown key {dotted!r}: {'.'.join(path)} is a single value, it has no sub-keys")
    return path, current, optional


def _structured(raw: str, what: str, expect: type) -> Any:
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise KeyValueError(f"{what}: expected YAML/JSON, e.g. '{{key: value}}'") from None
    if not isinstance(value, expect):
        raise KeyValueError(f"{what}: expected a {'mapping' if expect is dict else 'list'}, got {raw!r}")
    return value


def coerce(annotation: Any, raw: str, *, optional: bool = False, key: str = "value") -> Any:
    """Text -> the Python value the field wants. Range and pattern checks are left to the model."""
    text = raw.strip()
    if optional and text.lower() in (UNSET_WORD, ""):
        return None
    origin = get_origin(annotation)
    if origin is Literal:
        choices = [str(c) for c in get_args(annotation)]
        if text not in choices:
            raise KeyValueError(f"{key}: {raw!r} is not one of {', '.join(choices)}")
        return text
    if annotation is str:
        return raw
    if annotation is bool:
        if text.lower() in _TRUE:
            return True
        if text.lower() in _FALSE:
            return False
        raise KeyValueError(f"{key}: expected true or false, got {raw!r}")
    if annotation is int:
        try:
            return int(text, 10)
        except ValueError:
            raise KeyValueError(f"{key}: expected a whole number, got {raw!r}") from None
    if annotation is float:
        try:
            return float(text)
        except ValueError:
            raise KeyValueError(f"{key}: expected a number, got {raw!r}") from None
    if origin is list:
        if text.startswith("["):
            return _structured(text, key, list)
        return [item.strip() for item in text.split(",") if item.strip()]
    if origin is dict or _is_model(annotation):
        return _structured(text, key, dict)
    raise KeyValueError(f"{key}: values of this kind cannot be set from the command line")


def nest(path: list[str], value: Any) -> dict:
    """['network', 'mode'], 'vpn' -> {'network': {'mode': 'vpn'}}"""
    out: Any = value
    for part in reversed(path):
        out = {part: out}
    return out


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def parse_assignment(model: type[BaseModel], assignment: str) -> dict:
    """One `key=value` -> a nested patch for `model`."""
    key, sep, raw = assignment.partition("=")
    if not sep or not key.strip():
        raise KeyValueError(f"expected key=value, got {assignment!r}")
    key = key.strip()
    path, annotation, optional = locate(model, key)
    if _is_model(annotation) and not raw.strip().startswith("{"):
        raise KeyValueError(f"{key} is a group of settings; set one of: "
                          f"{', '.join(keys_of(annotation, key + '.'))}")
    if get_origin(annotation) is dict and raw.strip() in ("", "{}"):
        return nest(path, None)                 # in a patch, null is what removes a whole map
    return nest(path, coerce(annotation, raw, optional=optional, key=key))


def _empties_and_fills(a: dict, b: dict) -> bool:
    """Does one patch remove a map (null) that the other puts entries into?"""
    for key in a.keys() & b.keys():
        x, y = a[key], b[key]
        if isinstance(x, dict) and isinstance(y, dict):
            if _empties_and_fills(x, y):
                return True
        elif (x is None and isinstance(y, dict)) or (y is None and isinstance(x, dict)):
            return True
    return False


def parse_assignments(model: type[BaseModel], assignments: list[str]) -> dict:
    """Several `key=value` -> one nested patch. A key given twice keeps the last value."""
    patch: dict = {}
    for item in assignments:
        one = parse_assignment(model, item)
        if _empties_and_fills(patch, one):
            raise KeyValueError(f"{item.partition('=')[0].strip()}: a map cannot be emptied and given entries by one "
                                "patch (the entries would be merged into what is there). Do it in two commands")
        patch = merge(patch, one)
    return patch
