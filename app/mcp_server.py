"""MCP server exposing the extractor to any MCP client (Claude Code, Claude Desktop, ...).

Run locally (stdio):       .venv/bin/python -m app.mcp_server
Run remotely (HTTP):       .venv/bin/python -m app.mcp_server --http --host 0.0.0.0 --port 8765
                           -> endpoint http://<host>:8765/mcp
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import chat as chat_mod
from . import service
from .config import public_settings, update_settings
from .merge import simple_values

mcp = MCPServer("patient-data-extractor", instructions=(
    "Extracts a standard patient JSON record (with per-field provenance and conflicts) from healthcare "
    "documents grouped in sessions. Typical flow: create_session -> add_files -> get_record/get_conflicts -> "
    "add_custom_fields / ask_documents as needed -> publish (only when the user asks)."))


def _j(x: Any) -> str:
    return json.dumps(x, indent=1, default=str)


@mcp.tool()
def create_session(title: str | None = None) -> str:
    """Create a new extraction session (one patient). Returns the session id."""
    s = service.create_session(title)
    return _j({"session_id": s["id"], "title": s["title"]})


@mcp.tool()
def list_sessions() -> str:
    """List existing sessions."""
    return _j(service.list_sessions())


@mcp.tool()
def add_files(session_id: str, paths: list[str]) -> str:
    """Add local files (absolute paths: pdf, png/jpg, eml, docx, xlsx, csv, html, hl7, txt) to a session and extract them."""
    r = service.add_file_paths(session_id, paths)
    return _j({"documents": [{k: d.get(k) for k in ("id", "filename", "status", "document_type", "document_date", "error")}
                             for d in r["documents"]], "conflicts": r["conflicts"]})


@mcp.tool()
def get_record(session_id: str, detail: str = "values", section: str | None = None) -> str:
    """Get the merged patient record. detail='values' (plain standard JSON) or 'full' (provenance + conflict candidates)."""
    rec = service.get_record(session_id)
    data = rec["record"] if detail == "full" else simple_values(rec["record"])
    if section:
        data = {section: data.get(section)}
    return _j({"session_id": session_id, "record": data, "conflict_count": len(rec["conflicts"])})


@mcp.tool()
def get_conflicts(session_id: str) -> str:
    """List fields where documents disagree, with each candidate value and its source documents/dates."""
    return _j(service.get_record(session_id)["conflicts"])


@mcp.tool()
def list_documents(session_id: str) -> str:
    """List the documents in a session with type, date, summary and extraction status."""
    return _j(service.get_record(session_id)["documents"])


@mcp.tool()
def ask_documents(session_id: str, question: str) -> str:
    """Answer a free-form question by reading the original source documents of the session."""
    return service.ask_documents(session_id, question)


@mcp.tool()
def add_custom_fields(session_id: str, fields: list[dict[str, str]]) -> str:
    """Add non-standard data elements and extract them from all documents.
    Each field: {name, description, target} where target is 'record', 'patient', 'social_history' or a list
    section ('medications', 'allergies', 'conditions', 'lab_results', 'vitals', 'procedures', 'immunizations',
    'encounters', 'insurance', 'care_team') - list targets attach the element to every item."""
    return _j(service.add_custom_fields(session_id, fields))


@mcp.tool()
def set_field_value(session_id: str, path: str, value: str, note: str | None = None) -> str:
    """Record a user-confirmed value, e.g. path 'medications[metformin].dose' or 'patient.phone.mobile'."""
    return _j(service.set_override(session_id, path, value, note))


@mcp.tool()
def chat(session_id: str, message: str) -> str:
    """Send a message to the session's built-in chat assistant (same agent as the web portal)."""
    return chat_mod.chat(session_id, message)["content"]


@mcp.tool()
def publish(session_id: str, note: str | None = None) -> str:
    """Publish the current record to the configured webhook. Only call when the user explicitly asks to publish."""
    return _j(service.publish(session_id, note))


@mcp.tool()
def get_settings() -> str:
    """Show current provider/model/effort/webhook settings (API keys masked)."""
    s = public_settings()
    return _j({"llm": {k: s["llm"][k] for k in ("provider", "model", "effort", "max_tokens", "api_key_status")},
               "webhook": s["webhook"]})


@mcp.tool()
def update_settings_tool(provider: str | None = None, model: str | None = None, effort: str | None = None,
                         webhook_url: str | None = None) -> str:
    """Change provider/model/effort or the publish webhook URL (persisted to config/settings.json)."""
    patch: dict[str, Any] = {"llm": {}, "webhook": {}}
    for k, v in (("provider", provider), ("model", model), ("effort", effort)):
        if v:
            patch["llm"][k] = v
    if webhook_url:
        patch["webhook"]["url"] = webhook_url
    update_settings(patch)
    return get_settings()


def main():
    import argparse

    p = argparse.ArgumentParser(description="Patient data extractor MCP server")
    p.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    if a.http:
        mcp.run("streamable-http", host=a.host, port=a.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
