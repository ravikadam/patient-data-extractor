"""Merge per-document extractions into one patient record with provenance and conflicts.

Every leaf in the merged record is a *field node*:

    {"value": <chosen value>,
     "conflict": false,
     "sources": [{"doc_id", "filename", "document_date", "document_type"}]}

When sources disagree (after normalisation) the node becomes

    {"value": <value from the most recent document>,
     "conflict": true,
     "candidates": [{"value": ..., "sources": [...]}, ...],   # newest first
     "sources": [... all ...]}

A user override (set through chat) wins and is recorded as a candidate with source "user".
The merge is recomputed from the stored extractions every time, so adding documents or custom
fields never loses earlier evidence.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .schema import LIST_SECTIONS, OBJECT_SECTIONS

_MED_NOISE = {"hcl", "hydrochloride", "sodium", "potassium", "calcium", "succinate", "tartrate", "besylate", "maleate", "mesylate", "citrate", "sulfate", "acetate", "magnesium", "er", "xr", "xl", "sr", "dr", "tablet", "tablets", "tab", "oral"}
# Free-text descriptive fields: differing wording is reported as "variants", not as a data conflict.
DESCRIPTIVE_FIELDS = {"role", "notes", "summary", "indication", "reason", "specialty", "organization", "disposition",
                      "facility", "performer", "lab", "living_situation", "occupation", "plan_name", "brand_name"}
_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d", "%Y%m%d", "%m-%d-%Y"]


# ---------------------------------------------------------------------------------- normalise
def norm_date(v: str) -> str | None:
    s = str(v).strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return s
    return None


def norm_value(field: str, v: Any) -> str:
    if isinstance(v, (int, float, bool)):
        return str(v).lower()
    s = str(v).strip()
    if "date" in field or field in ("date", "date_of_birth"):
        d = norm_date(s)
        if d:
            return d
    if "phone" in field or field in ("home", "mobile", "work"):
        digits = re.sub(r"\D", "", s)
        if len(digits) >= 10:
            return digits[-10:]
    s = s.lower()
    s = re.sub(r"(\d)\s+(?=[a-z%])", r"\1", s)        # "500 mg" -> "500mg"
    s = re.sub(r"[^\w%./]+", " ", s)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)        # drop dots except decimal points
    return re.sub(r"\s+", " ", s).strip()


def entity_key_part(section: str, field: str, v: Any) -> str:
    if v is None or v == "":
        return ""
    if "date" in field:
        return norm_date(v) or norm_value(field, v)
    s = re.sub(r"[^a-z0-9 ]", " ", str(v).lower())
    words = [w for w in s.split() if w]
    if section == "medications":
        words = [w for w in words if w not in _MED_NOISE and not re.fullmatch(r"\d+(mg|mcg|g|ml)?", w)] or words
    if section == "care_team":
        words = [w for w in words if w not in {"dr", "md", "do", "rn", "np", "pa", "mr", "mrs", "ms", "phd", "facc"}] or words
    if section == "allergies" and words and words[-1].endswith("s") and len(words[-1]) > 4:
        words[-1] = words[-1][:-1]               # "penicillins" ~ "penicillin"
    if section == "lab_results":
        joined = "".join(words)
        aliases = {"hemoglobina1c": "hba1c", "a1c": "hba1c", "glycatedhemoglobin": "hba1c", "hgba1c": "hba1c",
                   "ldl": "ldlcholesterol", "ldlc": "ldlcholesterol", "egfr": "egfr", "estimatedgfr": "egfr"}
        return aliases.get(joined, joined)
    return " ".join(words)


def _present(v: Any) -> bool:
    return v not in (None, "", [], {}) and not (isinstance(v, str) and v.strip().lower() in ("null", "none", "n/a", "unknown", "not stated"))


# ---------------------------------------------------------------------------------- collect
class _Acc:
    """Accumulates candidate values per leaf path."""

    def __init__(self):
        self.leaves: dict[tuple, dict[str, dict[str, Any]]] = {}   # path -> norm -> {value, sources}
        self.order: list[tuple] = []

    def add(self, path: tuple, field: str, value: Any, src: dict[str, Any], norm: str | None = None):
        if not _present(value):
            return
        bucket = self.leaves.get(path)
        if bucket is None:
            bucket = self.leaves[path] = {}
            self.order.append(path)
        n = norm or norm_value(field, value if not isinstance(value, (list, dict)) else str(value))
        cand = bucket.setdefault(n, {"value": value, "sources": []})
        if src["doc_id"] == "user":
            cand["value"] = value  # the user's wording wins for an equivalent value
        if all(s["doc_id"] != src["doc_id"] for s in cand["sources"]):
            cand["sources"].append(src)


def _walk_object(acc: _Acc, prefix: tuple, obj: Any, src: dict[str, Any]):
    if not isinstance(obj, dict):
        return
    for k, v in obj.items():
        if isinstance(v, dict):
            _walk_object(acc, prefix + (k,), v, src)
        else:
            acc.add(prefix + (k,), k, v, src)


def _src_sort_key(src: dict[str, Any]) -> str:
    return src.get("document_date") or src.get("uploaded_at") or ""


def _is_identity(path: tuple) -> bool:
    meta = LIST_SECTIONS.get(path[0])
    return bool(meta) and len(path) == 3 and path[2] in meta["key"] + meta.get("fallback_key", [])


def _node(bucket: dict[str, dict[str, Any]], field: str, identity: bool = False) -> dict[str, Any]:
    cands = list(bucket.values())
    for c in cands:
        c["sources"].sort(key=_src_sort_key, reverse=True)
    # user override first, then newest supporting document, then most corroborated
    cands.sort(key=lambda c: (any(s["doc_id"] == "user" for s in c["sources"]),
                              max(_src_sort_key(s) for s in c["sources"]), len(c["sources"])), reverse=True)
    all_src = [s for c in cands for s in c["sources"]]
    resolved = any(s["doc_id"] == "user" for s in cands[0]["sources"])
    differs = len(cands) > 1 and not resolved
    descriptive = field in DESCRIPTIVE_FIELDS or identity
    node = {"value": cands[0]["value"], "conflict": differs and not descriptive, "sources": all_src}
    if differs and descriptive:
        node["variants"] = True
    if len(cands) > 1:
        node["candidates"] = cands
    if resolved:
        node["resolved_by"] = "user"
    return node


def _set(tree: dict, path: tuple, node: Any):
    cur = tree
    for p in path[:-1]:
        cur = cur.setdefault(p, {})
    cur[path[-1]] = node


# ---------------------------------------------------------------------------------- public
def path_str(path: tuple) -> str:
    """('medications', 'metformin', 'dose') -> 'medications[metformin].dose'."""
    out = path[0]
    rest = path[1:]
    if path[0] in LIST_SECTIONS and rest:
        out += f"[{rest[0]}]"
        rest = rest[1:]
    return ".".join([out, *rest]) if rest else out


def _collapse(bucket: dict[str, dict[str, Any]], mapping: dict[str, str] | None) -> dict[str, dict[str, Any]]:
    """Fold candidates the LLM judged clinically equivalent into their canonical candidate."""
    if not mapping:
        return bucket
    out: dict[str, dict[str, Any]] = {}
    for n, c in bucket.items():
        canon = mapping.get(n, n)
        if canon not in bucket:
            canon = n
        tgt = out.setdefault(canon, {"value": bucket[canon]["value"], "sources": []})
        if n != canon:
            tgt.setdefault("also_written_as", []).append(c["value"])
        for src in c["sources"]:
            if all(x["doc_id"] != src["doc_id"] for x in tgt["sources"]):
                tgt["sources"].append(src)
        if any(src["doc_id"] == "user" for src in c["sources"]):
            tgt["value"] = c["value"]
    return out


def ambiguous_fields(session: dict[str, Any], extractions: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Fields with differing values whose equivalence has not been judged yet."""
    acc, _ = _collect(session, extractions)
    eq = session.get("equivalences", {})
    out = []
    for path in acc.order:
        bucket = acc.leaves[path]
        if len(bucket) < 2 or any(src["doc_id"] == "user" for c in bucket.values() for src in c["sources"]):
            continue
        ps = path_str(path)
        if all(n in eq.get(ps, {}) for n in bucket):
            continue
        out.append({"path": ps, "candidates": [{"norm": n, "value": c["value"],
                                                "sources": [f"{x['filename']} ({x.get('document_date') or 'undated'})" for x in c["sources"]]}
                                               for n, c in bucket.items()]})
    return out


def merge(session: dict[str, Any], extractions: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """extractions: doc_id -> list of extraction JSONs for that document."""
    acc, entities = _collect(session, extractions)
    eq = session.get("equivalences", {})
    record: dict[str, Any] = {}
    for path in acc.order:
        _set(record, path, _node(_collapse(acc.leaves[path], eq.get(path_str(path))), path[-1], _is_identity(path)))
    return _finish(record, entities)


def _person_aliases(extractions: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """Map a surname-only care-team key ("raman") to the unique full name sharing it ("priya raman")."""
    keys = {entity_key_part("care_team", "name", it.get("name"))
            for passes in extractions.values() for ex in passes
            for it in (ex.get("care_team") or []) if isinstance(it, dict)}
    full = [k for k in keys if k and " " in k]
    out = {}
    for k in keys:
        if k and " " not in k:
            matches = [f for f in full if f.split()[-1] == k]
            if len(matches) == 1:
                out[k] = matches[0]
    return out


def _resolve_alias(mapping: dict[str, str], key: str) -> str:
    for _ in range(5):  # follow short alias chains
        nxt = mapping.get(key)
        if not nxt or nxt == key:
            break
        key = nxt
    return key


def entity_index(session: dict[str, Any], extractions: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Per list section: current entity keys with a short description (for LLM entity resolution)."""
    acc, entities = _collect(session, extractions)
    out: dict[str, list[dict[str, Any]]] = {}
    for name, ents in entities.items():
        rows = []
        for ekey in ents:
            desc = {}
            for f in list(LIST_SECTIONS[name]["fields"])[:5]:
                bucket = acc.leaves.get((name, ekey, f))
                if bucket:
                    desc[f] = next(iter(bucket.values()))["value"]
            rows.append({"key": ekey, "description": desc})
        if len(rows) > 1:
            out[name] = rows
    return out


def _collect(session: dict[str, Any], extractions: dict[str, list[dict[str, Any]]]):
    docs = {d["id"]: d for d in session["documents"]}
    aliases = _person_aliases(extractions)
    ent_aliases = session.get("entity_aliases", {})  # LLM entity-resolution judgements
    acc = _Acc()
    entities: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in LIST_SECTIONS}

    for doc_id, passes in extractions.items():
        d = docs.get(doc_id)
        if not d:
            continue
        src = {"doc_id": doc_id, "filename": d["filename"], "document_date": d.get("document_date"),
               "document_type": d.get("document_type"), "uploaded_at": d.get("uploaded_at")}
        for ex in passes:
            _walk_object(acc, ("custom",), ex.get("custom"), src)
            for name in OBJECT_SECTIONS:
                _walk_object(acc, (name,), ex.get(name), src)
            for name, meta in LIST_SECTIONS.items():
                items = ex.get(name)
                if isinstance(items, dict):
                    items = [items]
                for item in items or []:
                    if not isinstance(item, dict):
                        continue
                    key_fields = meta["key"]
                    if not entity_key_part(name, key_fields[0], item.get(key_fields[0])) and meta.get("fallback_key"):
                        key_fields = meta["fallback_key"]
                    key_parts = [entity_key_part(name, k, item.get(k)) for k in key_fields]
                    if not key_parts[0]:
                        continue
                    ekey = "|".join(p for p in key_parts if p) if len(key_parts) > 1 else key_parts[0]
                    if name == "care_team":
                        ekey = aliases.get(ekey, ekey)
                    ekey = _resolve_alias(ent_aliases.get(name, {}), ekey)
                    ent = entities[name].setdefault(ekey, {"entity_key": ekey, "_sources": []})
                    if all(s["doc_id"] != doc_id for s in ent["_sources"]):
                        ent["_sources"].append(src)
                    for k, v in item.items():
                        if isinstance(v, dict):
                            _walk_object(acc, (name, ekey, k), v, src)
                        elif k in key_fields:  # identity fields: spelling variants are not conflicts
                            acc.add((name, ekey, k), k, v, src, norm=entity_key_part(name, k, v))
                        else:
                            acc.add((name, ekey, k), k, v, src)

    # user overrides: path like "patient.phone.mobile" or "medications[metformin].dose"
    for ov in session.get("overrides", []):
        path = parse_path(ov["path"])
        if path[0] in LIST_SECTIONS and len(path) > 1:
            ent = entities[path[0]].setdefault(path[1], {"entity_key": path[1], "_sources": []})
        src = {"doc_id": "user", "filename": "user override (chat)", "document_date": ov["at"][:10],
               "document_type": "user override", "note": ov.get("note"), "uploaded_at": ov["at"]}
        acc.add(path, path[-1], ov["value"], src)
    return acc, entities


def _finish(record: dict[str, Any], entities: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:

    # turn list-section entity maps into lists
    for name in LIST_SECTIONS:
        items = []
        built = record.pop(name, {})
        for ekey, ent in entities[name].items():
            item = {"entity_key": ekey, "sources": sorted(ent["_sources"], key=_src_sort_key, reverse=True)}
            item.update(built.get(ekey, {}))
            items.append(item)
        if items:
            record[name] = items
    # stable section order
    order = ["patient"] + [n for n in LIST_SECTIONS] + ["social_history", "custom"]
    return {k: record[k] for k in order if k in record}


def parse_path(path: str) -> tuple:
    """'medications[metformin].dose' -> ('medications', 'metformin', 'dose')."""
    out: list[str] = []
    for part in re.findall(r"[^.\[\]]+|\[[^\]]*\]", path):
        out.append(part[1:-1] if part.startswith("[") else part)
    if not out:
        raise ValueError(f"bad path {path!r}")
    return tuple(out)


def simple_values(record: dict[str, Any]) -> dict[str, Any]:
    """Collapse field nodes to their chosen values (the plain standard JSON shape)."""
    def walk(x):
        if isinstance(x, dict):
            if "value" in x and "sources" in x and "conflict" in x:
                return x["value"]
            return {k: walk(v) for k, v in x.items() if k not in ("sources",)}
        if isinstance(x, list):
            return [walk(i) for i in x]
        return x
    return walk(record)


def list_conflicts(record: dict[str, Any]) -> list[dict[str, Any]]:
    out = []

    def walk(x, path):
        if isinstance(x, dict):
            if "conflict" in x and "sources" in x:
                if x["conflict"]:
                    out.append({"path": path, "chosen": x["value"],
                                "candidates": [{"value": c["value"],
                                                "sources": [f"{s['filename']} ({s.get('document_date') or 'undated'})" for s in c["sources"]]}
                                               for c in x["candidates"]]})
                return
            for k, v in x.items():
                if k in ("sources", "entity_key"):
                    continue
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(x, list):
            for item in x:
                walk(item, f"{path}[{item.get('entity_key')}]" if isinstance(item, dict) else path)

    walk(record, "")
    return out
