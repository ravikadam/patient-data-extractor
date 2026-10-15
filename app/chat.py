"""Chat agent for a session: the LLM drives the record through tools."""
from __future__ import annotations

import json
from typing import Any

from . import service, store
from .llm import ToolSpec, get_llm
from .merge import simple_values
from .schema import CUSTOM_TARGETS, LIST_SECTIONS, OBJECT_SECTIONS

SYSTEM = f"""You are the assistant inside a hospital "patient data extractor" workspace.
Each chat session gathers source documents about ONE patient (PDFs, scans/photos, e-mails, office
files, HL7...). Uploaded files are extracted automatically into the hospital's standard patient JSON
record; you see upload events in the conversation as "[event]" notes.

The merged record keeps provenance: every field lists the documents (filename + document date) it
came from. If documents disagree, the field is flagged as a conflict with every candidate value.

Use your tools:
- get_record / get_conflicts / list_documents to answer questions about the extracted data.
- ask_documents for anything not in the structured record (it reads the original files).
- add_custom_fields when the user wants data that is not in the standard schema. Choose the target
  where it belongs: {', '.join(CUSTOM_TARGETS)}. List targets attach the element to every item of that
  list (e.g. target "medications" for "pharmacy that filled each medication"); "record" is the root.
- set_field_value only when the user explicitly tells you which value is correct (e.g. resolves a conflict).
- publish ONLY when the user explicitly asks to publish/send/push the data.

Standard sections: object sections {list(OBJECT_SECTIONS)}; list sections {list(LIST_SECTIONS)}.
Field paths look like "patient.phone.mobile" or "medications[metformin].dose" (entity_key in brackets).

Style: concise, clinical, factual. When reporting a value that has a conflict, show each candidate
with its source document and date. Never invent data. Use markdown tables/bullets where helpful."""

TOOLS = [
    ToolSpec("get_record", "Get the merged patient record. Use detail='values' for the plain standard JSON, "
             "'full' to include provenance and conflict candidates. Optionally restrict to one section.",
             {"type": "object", "properties": {
                 "detail": {"type": "string", "enum": ["values", "full"]},
                 "section": {"type": "string", "description": "optional section name, e.g. medications"}},
              "required": ["detail"]}),
    ToolSpec("get_conflicts", "List every field where source documents disagree, with candidates and sources.",
             {"type": "object", "properties": {}}),
    ToolSpec("list_documents", "List documents in the session with type, date, summary and status.",
             {"type": "object", "properties": {}}),
    ToolSpec("ask_documents", "Answer a free-form question by reading the original source documents.",
             {"type": "object", "properties": {
                 "question": {"type": "string"},
                 "doc_ids": {"type": "array", "items": {"type": "string"}, "description": "optional subset"}},
              "required": ["question"]}),
    ToolSpec("add_custom_fields", "Add custom data elements to the record and extract them from all documents.",
             {"type": "object", "properties": {"fields": {"type": "array", "items": {"type": "object", "properties": {
                 "name": {"type": "string", "description": "snake_case name"},
                 "description": {"type": "string", "description": "what to extract, precisely"},
                 "target": {"type": "string", "enum": CUSTOM_TARGETS},
                 "type": {"type": "string", "description": "string | number | date | boolean | list"}},
                 "required": ["name", "description", "target"]}}},
              "required": ["fields"]}),
    ToolSpec("set_field_value", "Record a user-confirmed value for a field (wins over document values).",
             {"type": "object", "properties": {
                 "path": {"type": "string"}, "value": {"type": "string"}, "note": {"type": "string"}},
              "required": ["path", "value"]}),
    ToolSpec("publish", "Send the current record to the configured webhook (only on explicit user request).",
             {"type": "object", "properties": {"note": {"type": "string"}}}),
]


def _execute(sid: str, name: str, inp: dict[str, Any]) -> str:
    if name == "get_record":
        rec = service.get_record(sid)["record"]
        if inp.get("section"):
            rec = {inp["section"]: rec.get(inp["section"])}
        if inp.get("detail") != "full":
            rec = simple_values(rec)
        return json.dumps(rec, indent=1, default=str)
    if name == "get_conflicts":
        return json.dumps(service.get_record(sid)["conflicts"], indent=1)
    if name == "list_documents":
        return json.dumps(service.get_record(sid)["documents"], indent=1)
    if name == "ask_documents":
        return service.ask_documents(sid, inp["question"], inp.get("doc_ids"))
    if name == "add_custom_fields":
        return json.dumps(service.add_custom_fields(sid, inp["fields"]), indent=1, default=str)
    if name == "set_field_value":
        return json.dumps(service.set_override(sid, inp["path"], inp["value"], inp.get("note")))
    if name == "publish":
        return json.dumps(service.publish(sid, inp.get("note")))
    raise ValueError(f"unknown tool {name}")


def _history(s: dict[str, Any], limit: int = 40) -> list[dict[str, str]]:
    hist = []
    for m in s["messages"][-limit:]:
        if m["role"] == "event":
            hist.append({"role": "user", "content": f"[event] {m['content']}"})
        elif m["role"] in ("user", "assistant") and m["content"]:
            hist.append({"role": m["role"], "content": m["content"]})
    while hist and hist[0]["role"] != "user":
        hist.pop(0)
    return hist


def chat(sid: str, message: str) -> dict[str, Any]:
    with store.mutate(sid) as s:
        s["messages"].append({"role": "user", "content": message, "ts": store.now_iso()})
    s = store.get_session(sid)
    result = get_llm().run_tools(SYSTEM, _history(s), TOOLS, lambda n, i: _execute(sid, n, i))
    reply = {"role": "assistant", "content": result.text or "(no answer)", "ts": store.now_iso(),
             "tool_calls": result.tool_calls}
    with store.mutate(sid) as s:
        s["messages"].append(reply)
    return reply
