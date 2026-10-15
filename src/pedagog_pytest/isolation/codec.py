"""Plain-data codec shared by the grader and the runner.

Only exact builtin value types cross the process boundary by value: None, bool,
int, float, str, bytes, list, tuple, dict, set and frozenset. Decoding builds
nothing but those builtins, so data from student code can't run anything in
the grader (no pickle). Student objects travel as ``{"$ref": handle}``, integer
handles into the runner's object table.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

MAX_DEPTH = 64

_SCALARS = (type(None), bool, int, float, str)


class NotPlain(Exception):
    """The value isn't plain data (or is too deeply nested / cyclic)."""


def encode(obj: object, on_other: Callable[[object], dict] | None = None) -> str:
    """JSON text for ``obj``. Non-plain values go to ``on_other`` (which returns a
    placeholder) or raise NotPlain."""

    def walk(value: object, depth: int) -> object:
        if depth > MAX_DEPTH:
            raise NotPlain("nested too deeply")
        kind = type(value)
        if kind in _SCALARS:
            return value
        if kind is list:
            return [walk(v, depth + 1) for v in value]
        if kind is tuple:
            return {"$tuple": [walk(v, depth + 1) for v in value]}
        if kind is dict:
            return {"$dict": [[walk(k, depth + 1), walk(v, depth + 1)] for k, v in value.items()]}
        if kind is set:
            return {"$set": [walk(v, depth + 1) for v in value]}
        if kind is frozenset:
            return {"$frozenset": [walk(v, depth + 1) for v in value]}
        if kind is bytes:
            return {"$bytes": base64.b64encode(value).decode("ascii")}
        if on_other is not None:
            return on_other(value)
        raise NotPlain(f"{kind.__name__} is not plain data")

    try:
        return json.dumps(walk(obj, 0), allow_nan=True)
    except RecursionError as e:
        raise NotPlain("nested too deeply") from e


def decode(text: str, resolve: Callable[[int], object] | None = None) -> object:
    """Rebuild a value. ``$ref`` handles go through ``resolve``; without one
    (the grader decoding student data) they're rejected."""

    def build(value: object) -> object:
        if type(value) is list:
            return [build(v) for v in value]
        if type(value) is dict:
            ((tag, inner),) = value.items()
            if tag == "$tuple":
                return tuple(build(v) for v in inner)
            if tag == "$dict":
                return {build(k): build(v) for k, v in inner}
            if tag == "$set":
                return {build(v) for v in inner}
            if tag == "$frozenset":
                return frozenset(build(v) for v in inner)
            if tag == "$bytes":
                return base64.b64decode(inner)
            if tag == "$ref":
                if resolve is None or type(inner) is not int:
                    raise ValueError("unexpected object reference")
                return resolve(inner)
            raise ValueError(f"unknown tag {tag!r}")
        return value

    return build(json.loads(text))
