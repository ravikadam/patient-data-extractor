"""The hospital's standard patient record.

Two kinds of sections:
  * OBJECT sections (patient, social_history): nested objects of scalar fields.
  * LIST sections (medications, lab_results, ...): lists of entities. Entities coming from
    different documents are matched by their identity `key` fields, so "Metformin" in a
    discharge summary and "metformin" in an e-mail become one entity whose attributes carry
    provenance from both.

Every section/entity may carry a `custom` object holding user-requested custom data elements.
"""
from __future__ import annotations

import copy
from typing import Any

OBJECT_SECTIONS: dict[str, dict[str, Any]] = {
    "patient": {
        "name": {"first": "string", "middle": "string", "last": "string", "suffix": "string"},
        "date_of_birth": "date YYYY-MM-DD",
        "sex": "male | female | other | unknown",
        "gender_identity": "string",
        "mrn": "medical record number",
        "other_identifiers": "string (e.g. 'SSN last4: 1234; Driver license ...')",
        "phone": {"home": "string", "mobile": "string", "work": "string"},
        "email": "string",
        "address": {"line1": "string", "line2": "string", "city": "string", "state": "string",
                    "postal_code": "string", "country": "string"},
        "preferred_language": "string",
        "marital_status": "string",
        "race": "string",
        "ethnicity": "string",
        "emergency_contact": {"name": "string", "relationship": "string", "phone": "string"},
        "primary_care_provider": "string",
        "blood_type": "string",
        "height": "string with unit",
        "weight": "string with unit",
    },
    "social_history": {
        "smoking_status": "string",
        "alcohol_use": "string",
        "drug_use": "string",
        "occupation": "string",
        "living_situation": "string",
    },
}

LIST_SECTIONS: dict[str, dict[str, Any]] = {
    "insurance": {
        "key": ["member_id"], "fallback_key": ["payer"],
        "fields": {"payer": "string", "plan_name": "string", "member_id": "string", "group_number": "string",
                   "subscriber_name": "string", "relationship_to_subscriber": "string",
                   "coverage_type": "primary | secondary", "effective_date": "date"},
    },
    "allergies": {
        "key": ["substance"],
        "fields": {"substance": "string", "reaction": "string", "severity": "mild | moderate | severe",
                   "status": "active | inactive", "noted_date": "date"},
    },
    "conditions": {
        "key": ["name"],
        "fields": {"name": "string (normalized clinical name)", "icd10": "string", "status": "active | resolved | history",
                   "onset_date": "date", "notes": "string"},
    },
    "medications": {
        "key": ["name"],
        "fields": {"name": "generic drug name, lowercase", "brand_name": "string", "dose": "e.g. 500 mg",
                   "route": "string", "frequency": "e.g. twice daily", "status": "active | discontinued | changed",
                   "start_date": "date", "end_date": "date", "prescriber": "string", "indication": "string"},
    },
    "vitals": {
        "key": ["type", "date"],
        "fields": {"type": "blood_pressure | heart_rate | temperature | respiratory_rate | spo2 | weight | height | bmi",
                   "value": "string", "unit": "string", "date": "date"},
    },
    "lab_results": {
        "key": ["test", "date"],
        "fields": {"test": "normalized test name e.g. HbA1c", "value": "string", "unit": "string",
                   "reference_range": "string", "flag": "H | L | N | critical", "date": "date (collection)",
                   "lab": "string"},
    },
    "procedures": {
        "key": ["name", "date"],
        "fields": {"name": "string", "cpt": "string", "date": "date", "performer": "string", "facility": "string"},
    },
    "immunizations": {
        "key": ["vaccine", "date"],
        "fields": {"vaccine": "string", "date": "date", "dose_number": "string", "lot": "string"},
    },
    "encounters": {
        "key": ["type", "start_date"],
        "fields": {"type": "inpatient | outpatient | emergency | telehealth | referral", "start_date": "date",
                   "end_date": "date", "facility": "string", "provider": "string", "reason": "string",
                   "disposition": "string"},
    },
    "care_team": {
        "key": ["name"],
        "fields": {"name": "string (person, no title)", "role": "string", "specialty": "string",
                   "organization": "string", "phone": "string"},
    },
}

SECTION_NAMES = list(OBJECT_SECTIONS) + list(LIST_SECTIONS)
CUSTOM_TARGETS = ["record"] + SECTION_NAMES  # where a custom data element may be attached


def extraction_template(custom_fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """JSON 'shape' shown to the LLM, with custom fields placed where they belong."""
    custom_fields = custom_fields or []
    tpl: dict[str, Any] = {
        "document": {"document_type": "e.g. discharge summary | lab report | email | insurance card | intake form | HL7 message | prescription",
                     "document_date": "date YYYY-MM-DD the document was authored/sent (not birth date)",
                     "author": "string", "organization": "string", "summary": "2-3 sentence summary"},
    }
    for name, fields in OBJECT_SECTIONS.items():
        tpl[name] = copy.deepcopy(fields)
    for name, meta in LIST_SECTIONS.items():
        tpl[name] = [copy.deepcopy(meta["fields"])]

    for cf in custom_fields:
        target, entry = cf["target"], f"{cf['description']} ({cf.get('type', 'string')})"
        if target == "record":
            tpl.setdefault("custom", {})[cf["name"]] = entry
        elif target in OBJECT_SECTIONS:
            tpl[target].setdefault("custom", {})[cf["name"]] = entry
        elif target in LIST_SECTIONS:
            tpl[target][0].setdefault("custom", {})[cf["name"]] = entry
    return tpl


def custom_template(custom_fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape for a targeted pass that extracts only custom fields (identity keys included for list entities)."""
    tpl: dict[str, Any] = {}
    for cf in custom_fields:
        target, entry = cf["target"], f"{cf['description']} ({cf.get('type', 'string')})"
        if target == "record":
            tpl.setdefault("custom", {})[cf["name"]] = entry
        elif target in OBJECT_SECTIONS:
            tpl.setdefault(target, {}).setdefault("custom", {})[cf["name"]] = entry
        elif target in LIST_SECTIONS:
            if target not in tpl:
                meta = LIST_SECTIONS[target]
                tpl[target] = [{k: meta["fields"][k] for k in meta["key"]}]
            tpl[target][0].setdefault("custom", {})[cf["name"]] = entry
    return tpl
