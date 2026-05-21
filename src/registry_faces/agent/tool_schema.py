"""Introspect a Python function and return a JSON Schema describing it.

Used by providers that need to send tool definitions to the model server-side
(OpenAI-compatible APIs). The Anthropic SDK auto-generates schemas via
`@beta_tool`, and the Google SDK auto-generates schemas via its automatic
function calling — neither needs this. The OpenAI-style providers do.

Docstring convention:
    First paragraph -> tool description.
    `Args:` block (Google style) -> per-parameter descriptions.
"""

from __future__ import annotations

import inspect
import types as _types
from collections.abc import Callable
from typing import Union, get_args, get_origin, get_type_hints

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}


def _py_type_to_json(t: object) -> str:
    origin = get_origin(t)
    # Handle Optional[X] / X | None
    if origin is Union or origin is _types.UnionType:
        non_none = [a for a in get_args(t) if a is not type(None)]
        if non_none:
            return _py_type_to_json(non_none[0])
    if origin is list:
        return "array"
    if origin is dict:
        return "object"
    return _PY_TO_JSON.get(t, "string")  # type: ignore[arg-type]


def _parse_args_section(doc: str) -> dict[str, str]:
    if "Args:" not in doc:
        return {}
    section = doc.split("Args:", 1)[1]
    for marker in ("\nReturns:", "\nRaises:", "\nYields:"):
        if marker in section:
            section = section.split(marker)[0]
            break

    out: dict[str, str] = {}
    current_name: str | None = None
    current_desc: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # New arg: starts with "name:" at a known indent
        if ":" in stripped and not stripped.startswith(("Returns", "Raises", "Yields")):
            head, _, rest = stripped.partition(":")
            # Heuristic: arg names don't contain spaces
            if " " not in head and head.isidentifier():
                if current_name is not None:
                    out[current_name] = " ".join(current_desc).strip()
                current_name = head
                current_desc = [rest.strip()]
                continue
        if current_name is not None:
            current_desc.append(stripped)
    if current_name is not None:
        out[current_name] = " ".join(current_desc).strip()
    return out


def tool_schema(fn: Callable) -> dict:
    """Return {name, description, parameters} for a Python function."""
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    doc = inspect.getdoc(fn) or ""
    description = doc.split("\n\nArgs:")[0].split("Args:")[0].strip()
    if not description:
        description = doc.strip().split("\n\n")[0] if doc else ""

    arg_docs = _parse_args_section(doc)

    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        py_type = hints.get(name, str)
        prop: dict[str, object] = {"type": _py_type_to_json(py_type)}
        if name in arg_docs:
            prop["description"] = arg_docs[name]
        properties[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "name": fn.__name__,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def tool_to_openai_schema(fn: Callable) -> dict:
    """OpenAI tool wire format: {type: function, function: {...}}."""
    return {"type": "function", "function": tool_schema(fn)}
