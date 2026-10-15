"""File-based session store (one folder per chat session), shared by every interface.

data/sessions/<session_id>/
    session.json                     metadata, documents, custom fields, overrides, chat, publishes
    files/<doc_id>__<filename>       original uploads (and e-mail attachments)
    text/<doc_id>.txt                plain-text rendering when available
    extractions/<doc_id>/*.json      one JSON per extraction pass over that document
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR

SESSIONS_DIR = DATA_DIR / "sessions"
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lock(sid: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(sid, threading.RLock())


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:150] or "file"


def session_dir(sid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid or ""):
        raise KeyError(f"invalid session id {sid!r}")
    return SESSIONS_DIR / sid


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(path)


def create_session(title: str | None = None) -> dict[str, Any]:
    sid = "ses_" + uuid.uuid4().hex[:12]
    s = {"id": sid, "title": title or f"Session {datetime.now().strftime('%Y-%m-%d %H:%M')}",
         "created_at": now_iso(), "updated_at": now_iso(), "documents": [], "custom_fields": [],
         "overrides": [], "messages": [], "publishes": []}
    _write_json(session_dir(sid) / "session.json", s)
    return s


def get_session(sid: str) -> dict[str, Any]:
    p = session_dir(sid) / "session.json"
    if not p.exists():
        raise KeyError(f"session {sid} not found")
    with _lock(sid):
        return json.loads(p.read_text())


@contextmanager
def mutate(sid: str) -> Iterator[dict[str, Any]]:
    """Read-modify-write a session under its lock."""
    with _lock(sid):
        s = get_session(sid)
        yield s
        s["updated_at"] = now_iso()
        _write_json(session_dir(sid) / "session.json", s)


def list_sessions() -> list[dict[str, Any]]:
    out = []
    if SESSIONS_DIR.exists():
        for p in SESSIONS_DIR.glob("*/session.json"):
            try:
                s = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            out.append({"id": s["id"], "title": s["title"], "created_at": s["created_at"],
                        "updated_at": s["updated_at"], "documents": len(s["documents"]),
                        "publishes": len(s.get("publishes", []))})
    return sorted(out, key=lambda x: x["updated_at"], reverse=True)


def delete_session(sid: str) -> None:
    import shutil

    shutil.rmtree(session_dir(sid))


def save_file(sid: str, filename: str, data: bytes, parent_id: str | None = None) -> dict[str, Any]:
    doc_id = "doc_" + uuid.uuid4().hex[:10]
    stored = f"{doc_id}__{_safe_name(filename)}"
    path = session_dir(sid) / "files" / stored
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    doc = {"id": doc_id, "filename": _safe_name(filename), "stored_name": stored, "size": len(data),
           "uploaded_at": now_iso(), "parent_id": parent_id, "status": "pending"}
    with mutate(sid) as s:
        s["documents"].append(doc)
    return doc


def file_bytes(sid: str, doc: dict[str, Any]) -> bytes:
    return (session_dir(sid) / "files" / doc["stored_name"]).read_bytes()


def file_path(sid: str, doc: dict[str, Any]) -> Path:
    return session_dir(sid) / "files" / doc["stored_name"]


def update_document(sid: str, doc_id: str, **fields: Any) -> None:
    with mutate(sid) as s:
        for d in s["documents"]:
            if d["id"] == doc_id:
                d.update(fields)


def save_text(sid: str, doc_id: str, text: str) -> None:
    p = session_dir(sid) / "text" / f"{doc_id}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def load_text(sid: str, doc_id: str) -> str | None:
    p = session_dir(sid) / "text" / f"{doc_id}.txt"
    return p.read_text() if p.exists() else None


def save_extraction(sid: str, doc_id: str, kind: str, data: dict[str, Any]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    _write_json(session_dir(sid) / "extractions" / doc_id / f"{stamp}_{kind}.json", data)


def load_extractions(sid: str, doc_id: str) -> list[dict[str, Any]]:
    d = session_dir(sid) / "extractions" / doc_id
    if not d.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]
