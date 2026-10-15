"""Application service: the single set of operations every interface (REST, MCP, CLI/skill, web UI) uses."""
from __future__ import annotations

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from . import store
from .config import load_settings
from .extractor import extract_custom, extract_document
from .ingest import prepare
from .llm import LLMError, get_llm
from .merge import ambiguous_fields, entity_index, list_conflicts, merge, norm_date, parse_path, simple_values
from .schema import CUSTOM_TARGETS, LIST_SECTIONS, OBJECT_SECTIONS

MAX_PARALLEL = 4


# ------------------------------------------------------------------------------ sessions
def create_session(title: str | None = None) -> dict[str, Any]:
    return store.create_session(title)


def list_sessions() -> list[dict[str, Any]]:
    return store.list_sessions()


def get_session(sid: str) -> dict[str, Any]:
    return store.get_session(sid)


def rename_session(sid: str, title: str) -> dict[str, Any]:
    with store.mutate(sid) as s:
        s["title"] = title
    return store.get_session(sid)


def delete_session(sid: str) -> None:
    store.delete_session(sid)


def add_event(sid: str, text: str) -> None:
    with store.mutate(sid) as s:
        s["messages"].append({"role": "event", "content": text, "ts": store.now_iso()})


# ------------------------------------------------------------------------------ documents
def _process(sid: str, doc: dict[str, Any], custom_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepare + extract one document. Returns docs to process next (e-mail attachments)."""
    children: list[dict[str, Any]] = []
    try:
        prepared = prepare(doc["filename"], store.file_bytes(sid, doc))
        if prepared.text:
            store.save_text(sid, doc["id"], prepared.text)
        for name, data in prepared.attachments:
            child = store.save_file(sid, name, data, parent_id=doc["id"])
            child["parent_filename"] = doc["filename"]
            child["hint_date"] = prepared.hint_date
            store.update_document(sid, child["id"], parent_filename=doc["filename"], hint_date=prepared.hint_date)
            children.append(child)
        hint = prepared.hint_date or doc.get("hint_date")
        ex = extract_document(get_llm(), doc, prepared.parts, hint, custom_fields)
        store.save_extraction(sid, doc["id"], "base", ex)
        meta = ex.get("document") or {}
        ddate = norm_date(meta.get("document_date") or "") or hint
        store.update_document(sid, doc["id"], status="extracted", document_type=meta.get("document_type"),
                              document_date=ddate, author=meta.get("author"), organization=meta.get("organization"),
                              summary=meta.get("summary"), hint_date=hint, error=None)
    except (LLMError, ValueError, OSError) as e:
        store.update_document(sid, doc["id"], status="error", error=str(e))
    except Exception as e:  # keep the batch going; surface the problem on the document
        store.update_document(sid, doc["id"], status="error", error=f"{type(e).__name__}: {e}")
    return children


def add_files(sid: str, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Store files, extract them (in parallel, including e-mail attachments) and return a summary."""
    store.get_session(sid)
    custom_fields = store.get_session(sid)["custom_fields"]
    queue = [store.save_file(sid, name, data) for name, data in files]
    new_ids = [d["id"] for d in queue]
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        while queue:
            batch, queue = queue, []
            for children in pool.map(lambda d: _process(sid, d, custom_fields), batch):
                queue.extend(children)
                new_ids.extend(c["id"] for c in children)
    recon_error = reconcile(sid)
    s = store.get_session(sid)
    docs = [d for d in s["documents"] if d["id"] in new_ids]
    rec = get_record(sid)
    lines = []
    for d in docs:
        tag = f"{d.get('document_type') or 'document'}, dated {d.get('document_date') or 'unknown'}" if d["status"] == "extracted" else f"ERROR: {d.get('error')}"
        lines.append(f"- {d['filename']} ({tag})" + (f" [attachment of {d.get('parent_filename')}]" if d.get("parent_id") else ""))
    add_event(sid, f"{len(docs)} document(s) processed:\n" + "\n".join(lines) +
              f"\nRecord now has {len(rec['conflicts'])} conflicting field(s)." +
              (f"\n(Reconciliation skipped: {recon_error})" if recon_error else ""))
    return {"documents": docs, "conflicts": rec["conflicts"], "record": rec["record"]}


def add_file_paths(sid: str, paths: list[str]) -> dict[str, Any]:
    from pathlib import Path

    files = []
    for p in paths:
        path = Path(p).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"not a file: {p}")
        files.append((path.name, path.read_bytes()))
    return add_files(sid, files)


def reprocess_document(sid: str, doc_id: str) -> dict[str, Any]:
    s = store.get_session(sid)
    doc = next(d for d in s["documents"] if d["id"] == doc_id)
    _process(sid, doc, s["custom_fields"])
    return next(d for d in store.get_session(sid)["documents"] if d["id"] == doc_id)


# ------------------------------------------------------------------------------ record
def _all_extractions(s: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {d["id"]: store.load_extractions(s["id"], d["id"]) for d in s["documents"] if d["status"] == "extracted"}


def get_record(sid: str) -> dict[str, Any]:
    s = store.get_session(sid)
    record = merge(s, _all_extractions(s))
    docs = [{k: d.get(k) for k in ("id", "filename", "document_type", "document_date", "author", "organization",
                                    "summary", "status", "error", "parent_id", "uploaded_at")} for d in s["documents"]]
    return {"session_id": sid, "title": s["title"], "generated_at": store.now_iso(), "documents": docs,
            "custom_fields": s["custom_fields"], "overrides": s.get("overrides", []),
            "record": record, "conflicts": list_conflicts(record)}


# ------------------------------------------------------------------------------ reconciliation
RECONCILE_SYSTEM = """You reconcile values for the same data element that came from different clinical documents.
For each item, group the candidate values that state the SAME clinical fact, differing only in wording,
formatting, abbreviation, completeness or added detail - e.g. "81" vs "81 mg" (aspirin dose), "daily" vs
"once daily", "nightly" vs "at bedtime", "M" vs "Maria" (middle name initial), "Sofia" vs "Sofia Vasquez",
"Dr. Raman" vs "Priya Raman, MD", "hives" vs "urticaria".
Do NOT group values that differ in substance: different doses or numbers, different dates, different
phone numbers or addresses, a different status (active vs discontinued vs changed), a different reaction
(hives vs lip swelling), or values where one could be a data-entry error (DOB 06/22 vs 06/23).
When unsure, keep values separate - a false conflict is safer than a hidden one.
In each group put FIRST the most complete/specific wording.
Return ONLY JSON: {"items": [{"id": "<id>", "groups": [[<candidate index>, ...], ...]}]} covering every candidate index."""


ENTITY_SYSTEM = """You perform entity resolution on items abstracted from several clinical documents about ONE patient.
Within each list section, group item keys that refer to the SAME real-world item described differently:
the same drug (generic vs brand vs salt form, e.g. "entresto" = "sacubitril valsartan"), the same allergen
("sulfa" = "sulfonamides" = "sulfa bactrim"), the same clinician, the same insurance policy, the same
condition ("chf" = "heart failure with reduced ejection fraction"), the same encounter.
Do NOT group: different drugs (even same class), a combination product with one of its components,
lab results or vitals with different dates, different procedures, different people.
When unsure, keep them separate. Put FIRST in each group the key with the most standard/complete name.
Return ONLY JSON: {"sections": {"<section>": [["<key>", "<key>", ...], ...]}} - include only groups with 2+ keys."""


def _resolve_entities(s: dict[str, Any], llm) -> None:
    index = entity_index(s, _all_extractions(s))
    judged = s.get("entity_judged", {})
    todo = {sec: rows for sec, rows in index.items() if any(r["key"] not in judged.get(sec, []) for r in rows)}
    if not todo:
        return
    out = llm.generate_json(ENTITY_SYSTEM, [{"type": "text", "text": json.dumps({"sections": todo}, indent=1, default=str)}])
    groups = (out or {}).get("sections", {}) if isinstance(out, dict) else {}
    with store.mutate(s["id"]) as s2:
        aliases = s2.setdefault("entity_aliases", {})
        seen = s2.setdefault("entity_judged", {})
        for sec, rows in todo.items():
            valid = {r["key"] for r in rows}
            for group in groups.get(sec, []) or []:
                keys = [k for k in group if k in valid]
                for k in keys[1:]:
                    aliases.setdefault(sec, {})[k] = keys[0]
            seen[sec] = sorted(set(seen.get(sec, [])) | valid)


def reconcile(sid: str) -> str | None:
    """LLM passes, cached on the session: (1) entity resolution across documents, (2) which differing
    values are clinically equivalent. Returns an error string if a pass could not run (the record is
    still valid, just noisier)."""
    entity_error = None
    try:
        _resolve_entities(store.get_session(sid), get_llm())
    except (LLMError, ValueError, KeyError, TypeError) as e:
        entity_error = f"entity resolution: {e}"  # still try the value pass below
    s = store.get_session(sid)
    pending = ambiguous_fields(s, _all_extractions(s))
    if not pending:
        return entity_error
    try:
        llm = get_llm()
        learned: dict[str, dict[str, str]] = {}
        for start in range(0, len(pending), 60):
            batch = pending[start:start + 60]
            items = [{"id": str(i), "field": f["path"], "candidates": [c["value"] for c in f["candidates"]]}
                     for i, f in enumerate(batch)]
            out = llm.generate_json(RECONCILE_SYSTEM, [{"type": "text", "text": json.dumps({"items": items}, indent=1)}])
            for it in (out or {}).get("items", []):
                f = batch[int(it["id"])]
                norms = [c["norm"] for c in f["candidates"]]
                mapping = {n: n for n in norms}
                for group in it.get("groups", []):
                    idx = [i for i in group if isinstance(i, int) and 0 <= i < len(norms)]
                    for i in idx:
                        mapping[norms[i]] = norms[idx[0]]
                learned[f["path"]] = mapping
    except (LLMError, ValueError, KeyError, TypeError) as e:
        return str(e)
    with store.mutate(sid) as s:
        eq = s.setdefault("equivalences", {})
        for path, mapping in learned.items():
            eq.setdefault(path, {}).update(mapping)
    return entity_error


# ------------------------------------------------------------------------------ custom data
def add_custom_fields(sid: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Register custom data elements and extract them from every document already in the session."""
    clean = []
    for f in fields:
        name = re.sub(r"[^a-z0-9_]", "_", str(f.get("name", "")).strip().lower()).strip("_")
        target = f.get("target") or "record"
        if not name:
            raise ValueError("custom field needs a name")
        if target not in CUSTOM_TARGETS:
            raise ValueError(f"target must be one of {CUSTOM_TARGETS}")
        clean.append({"name": name, "description": f.get("description") or name, "target": target,
                      "type": f.get("type") or "string", "added_at": store.now_iso()})
    with store.mutate(sid) as s:
        existing = {(c["name"], c["target"]) for c in s["custom_fields"]}
        s["custom_fields"].extend(c for c in clean if (c["name"], c["target"]) not in existing)
    s = store.get_session(sid)
    docs = [d for d in s["documents"] if d["status"] == "extracted"]

    def run(doc):
        prepared = prepare(doc["filename"], store.file_bytes(sid, doc))
        ex = extract_custom(get_llm(), doc, prepared.parts, doc.get("hint_date"), clean)
        if ex:
            store.save_extraction(sid, doc["id"], "custom", ex)

    errors = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        for doc, fut in [(d, pool.submit(run, d)) for d in docs]:
            try:
                fut.result()
            except Exception as e:
                errors.append(f"{doc['filename']}: {e}")
    reconcile(sid)
    rec = get_record(sid)
    found = _custom_values(rec["record"], clean)
    add_event(sid, "Custom data element(s) added: " + ", ".join(f"{c['name']} @ {c['target']}" for c in clean) +
              (f" (errors: {'; '.join(errors)})" if errors else ""))
    return {"custom_fields": clean, "values": found, "errors": errors}


def _custom_values(record: dict[str, Any], fields: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for f in fields:
        t, n = f["target"], f["name"]
        if t == "record":
            out[f"custom.{n}"] = (record.get("custom") or {}).get(n)
        elif t in OBJECT_SECTIONS:
            out[f"{t}.custom.{n}"] = ((record.get(t) or {}).get("custom") or {}).get(n)
        elif t in LIST_SECTIONS:
            for item in record.get(t, []):
                v = (item.get("custom") or {}).get(n)
                if v:
                    out[f"{t}[{item['entity_key']}].custom.{n}"] = v
    return out


def remove_custom_field(sid: str, name: str) -> None:
    with store.mutate(sid) as s:
        s["custom_fields"] = [c for c in s["custom_fields"] if c["name"] != name]


def set_override(sid: str, path: str, value: Any, note: str | None = None) -> dict[str, Any]:
    p = parse_path(path)
    if p[0] not in OBJECT_SECTIONS and p[0] not in LIST_SECTIONS and p[0] != "custom":
        raise ValueError(f"path must start with one of {list(OBJECT_SECTIONS) + list(LIST_SECTIONS) + ['custom']}")
    ov = {"path": path, "value": value, "note": note, "at": store.now_iso()}
    with store.mutate(sid) as s:
        s.setdefault("overrides", []).append(ov)
    return ov


# ------------------------------------------------------------------------------ Q&A over sources
ASK_SYSTEM = """You answer questions about a patient using ONLY the attached source documents.
Cite the filename (and document date if known) for every fact. If the documents do not contain
the answer, say so plainly. Point out when documents disagree."""


def ask_documents(sid: str, question: str, doc_ids: list[str] | None = None) -> str:
    s = store.get_session(sid)
    docs = [d for d in s["documents"] if d["status"] == "extracted" and (not doc_ids or d["id"] in doc_ids)]
    if not docs:
        return "No extracted documents in this session yet."
    parts: list[dict[str, Any]] = []
    for d in docs:
        parts.append({"type": "text", "text": f"=== DOCUMENT {d['id']} | {d['filename']} | type={d.get('document_type')} | date={d.get('document_date')} ==="})
        parts.extend(prepare(d["filename"], store.file_bytes(sid, d)).parts)
    parts.append({"type": "text", "text": f"QUESTION: {question}"})
    return get_llm().generate(ASK_SYSTEM, parts)


# ------------------------------------------------------------------------------ publish
def publish(sid: str, note: str | None = None, url: str | None = None) -> dict[str, Any]:
    settings = load_settings()["webhook"]
    url = url or settings.get("url")
    if not url:
        raise ValueError("No webhook URL configured. Set it in Settings (portal) or `hdx config --webhook URL`.")
    rec = get_record(sid)
    s = store.get_session(sid)
    seq = len(s.get("publishes", [])) + 1
    publish_id = "pub_" + uuid.uuid4().hex[:12]
    payload = {
        "event": "patient_record.published",
        "session_id": sid,
        "publish_id": publish_id,
        "publish_sequence": seq,
        "is_update": seq > 1,
        "published_at": store.now_iso(),
        "session_title": s["title"],
        "note": note,
        "documents": rec["documents"],
        "custom_fields": rec["custom_fields"],
        "conflicts": rec["conflicts"],
        "record_values": simple_values(rec["record"]),
        "record": rec["record"],
    }
    entry = {"sequence": seq, "publish_id": publish_id, "at": payload["published_at"], "url": url, "note": note}
    try:
        r = httpx.post(url, json=payload, timeout=settings.get("timeout_seconds", 20),
                       headers={"X-HDX-Session-Id": sid, "X-HDX-Publish-Sequence": str(seq),
                                "X-HDX-Publish-Id": publish_id})
        entry.update(status_code=r.status_code, ok=r.is_success)
    except httpx.HTTPError as e:
        entry.update(status_code=None, ok=False, error=str(e))
    with store.mutate(sid) as s2:
        s2.setdefault("publishes", []).append(entry)
    add_event(sid, f"Publish #{seq} to webhook -> {'OK' if entry['ok'] else 'FAILED'} "
                   f"(HTTP {entry.get('status_code')}){' ' + entry['error'] if entry.get('error') else ''}")
    return entry
