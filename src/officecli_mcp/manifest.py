"""Build the tool manifest from the FastMCP registry.

Single source of truth: the manifest is derived from the same FastMCP
instance that serves /mcp, so the shim's embedded docs can never drift from
the real tools. Consumed by the /tools endpoint (server.py), the shim
renderer (shim.py) and the OpenWebUI self-sync (shim_sync.py).
"""
from __future__ import annotations

import hashlib
import json
import logging

from mcp.shared.memory import create_connected_server_and_client_session

log = logging.getLogger(__name__)

_JSON_TYPE = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


def _compact_type(schema: dict) -> str:
    # Handle anyOf: pick the first non-null variant
    any_of = schema.get("anyOf")
    if any_of:
        non_null = [s for s in any_of if s.get("type") != "null"]
        if non_null:
            return _compact_type(non_null[0])
        return "Any"
    t = schema.get("type")
    if t == "array":
        inner = _JSON_TYPE.get((schema.get("items") or {}).get("type"), "Any")
        return f"list[{inner}]"
    return _JSON_TYPE.get(t, "Any")


def _is_null_type(schema: dict) -> bool:
    """True if the schema is only 'null' type (not anyOf with a real type)."""
    t = schema.get("type")
    if t == "null":
        return True
    any_of = schema.get("anyOf")
    if any_of and all(s.get("type") == "null" for s in any_of):
        return True
    return False


def _compact_sig(schema: dict) -> str:
    """'(file_id: str, selector: str, prop?: list[str])' from an inputSchema."""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts = [
        f"{name}{'' if name in required or _is_null_type(s) else '?'}: {_compact_type(s)}"
        for name, s in props.items()
    ]
    return f"({', '.join(parts)})"


def manifest_revision(tools: list[dict]) -> str:
    """sha1 over the sorted (name, description, canonical schema) tuples."""
    h = hashlib.sha1()  # noqa: S324 - identity stamp, not security
    for t in sorted(tools, key=lambda x: x["name"]):
        h.update(t["name"].encode())
        h.update(b"\0")
        h.update((t.get("description") or "").encode())
        h.update(b"\0")
        h.update(
            json.dumps(t.get("inputSchema") or {}, sort_keys=True).encode()
        )
        h.update(b"\0")
    return h.hexdigest()


async def get_manifest(mcp) -> dict:
    """Return {revision, instructions, tools} for a built FastMCP instance."""
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.list_tools()
    tools = []
    for t in result.tools:
        schema = t.inputSchema or {}
        ann = t.annotations
        tools.append(
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": schema,
                "readOnly": bool(getattr(ann, "readOnlyHint", False)),
                "signature": f"{t.name}{_compact_sig(schema)}",
            }
        )
    return {
        "revision": manifest_revision(tools),
        "instructions": mcp.instructions or "",
        "tools": tools,
    }
