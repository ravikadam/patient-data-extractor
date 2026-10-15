"""LLM extraction: one document in -> partial standard patient record out."""
from __future__ import annotations

import json
from typing import Any

from .llm import LLMProvider
from .schema import custom_template, extraction_template

EXTRACTION_SYSTEM = """You are a meticulous clinical data abstraction specialist for a hospital system.
You read one source document at a time (it may be a PDF, a scanned image or photo, an e-mail, an
office document, a spreadsheet, an HL7 message or free text - structure is NOT standardised) and
abstract facts about the PATIENT into the hospital's standard JSON record.

Rules:
- Extract only what the document actually states. Never guess or fill from general knowledge.
- Omit any field (or whole section) that the document does not mention. No nulls, no empty strings.
- Facts about other people (relatives, the sender of an e-mail, clinicians) must not be put in
  patient fields; clinicians belong in care_team.
- Dates: ISO 8601 (YYYY-MM-DD). If only month/year, use YYYY-MM. Interpret relative dates
  ("yesterday") against the document date.
- Medications: `name` is the lowercase generic name ("metformin"), brand in brand_name. Keep the
  dose exactly as written (e.g. "1000 mg"). If the document says a dose was changed, report the NEW
  current dose and status "changed"; if stopped, status "discontinued".
- Lab tests: use common normalised names (HbA1c, LDL cholesterol, creatinine, eGFR, potassium...).
- Conditions: normalised clinical name; add ICD-10 only if the document states it.
- Copy identifiers (MRN, member id, group number, phone numbers) character-for-character.
- If something is ambiguous or illegible, leave it out.
- `custom` objects hold additional data elements the user requested; fill them only when the
  document supports a value.
Return ONLY one JSON object, no commentary."""


def _doc_header(doc: dict[str, Any], hint_date: str | None) -> str:
    h = f"SOURCE DOCUMENT: filename={doc['filename']!r}, uploaded_at={doc['uploaded_at']}"
    if hint_date:
        h += f", transport date (e-mail Date header)={hint_date}"
    if doc.get("parent_filename"):
        h += f", attached to e-mail {doc['parent_filename']!r}"
    return h


def extract_document(llm: LLMProvider, doc: dict[str, Any], parts: list[dict[str, Any]],
                     hint_date: str | None, custom_fields: list[dict[str, Any]]) -> dict[str, Any]:
    template = extraction_template(custom_fields)
    content = [{"type": "text", "text": _doc_header(doc, hint_date)}] + parts + [{
        "type": "text",
        "text": "Abstract this document into JSON with this shape (field descriptions are hints; lists may "
                "have any number of items; omit what is absent):\n" + json.dumps(template, indent=1),
    }]
    out = llm.generate_json(EXTRACTION_SYSTEM, content)
    if not isinstance(out, dict):
        raise ValueError("extraction did not return a JSON object")
    return out


def extract_custom(llm: LLMProvider, doc: dict[str, Any], parts: list[dict[str, Any]],
                   hint_date: str | None, custom_fields: list[dict[str, Any]]) -> dict[str, Any]:
    template = custom_template(custom_fields)
    content = [{"type": "text", "text": _doc_header(doc, hint_date)}] + parts + [{
        "type": "text",
        "text": "Extract ONLY these additional data elements from this document. For list sections, include the "
                "identity fields shown so the item can be matched to the record, and only items where the "
                "requested element is present. If nothing is present return {}.\n" + json.dumps(template, indent=1),
    }]
    out = llm.generate_json(EXTRACTION_SYSTEM, content)
    return out if isinstance(out, dict) else {}
