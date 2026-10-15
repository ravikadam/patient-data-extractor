"""REST API + the native chatbot portal (served at /)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import chat as chat_mod
from . import service, store
from .config import ROOT, public_settings, update_settings
from .llm import LLMError
from .merge import simple_values

app = FastAPI(title="Healthcare Document Extractor", version="0.1.0",
              description="Extract a standard patient JSON record from heterogeneous documents, per chat session.")
WEB = ROOT / "web"


@app.exception_handler(KeyError)
async def _not_found(_: Request, e: KeyError):
    return JSONResponse({"detail": str(e).strip("'\"")}, status_code=404)


@app.exception_handler(ValueError)
async def _bad(_: Request, e: ValueError):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(LLMError)
async def _llm(_: Request, e: LLMError):
    return JSONResponse({"detail": str(e)}, status_code=502)


# ------------------------------------------------------------------------------ models
class SessionIn(BaseModel):
    title: str | None = None


class ChatIn(BaseModel):
    message: str


class AskIn(BaseModel):
    question: str
    doc_ids: list[str] | None = None


class CustomFieldIn(BaseModel):
    name: str
    description: str
    target: str = "record"
    type: str = "string"


class CustomFieldsIn(BaseModel):
    fields: list[CustomFieldIn]


class OverrideIn(BaseModel):
    path: str
    value: Any
    note: str | None = None


class PublishIn(BaseModel):
    note: str | None = None


# ------------------------------------------------------------------------------ settings
@app.get("/api/settings")
def get_settings():
    return public_settings()


@app.put("/api/settings")
def put_settings(patch: dict[str, Any]):
    update_settings(patch)
    return public_settings()


# ------------------------------------------------------------------------------ sessions
@app.get("/api/sessions")
def sessions():
    return service.list_sessions()


@app.post("/api/sessions")
def new_session(body: SessionIn):
    return service.create_session(body.title)


@app.get("/api/sessions/{sid}")
def session(sid: str):
    return service.get_session(sid)


@app.patch("/api/sessions/{sid}")
def rename(sid: str, body: SessionIn):
    return service.rename_session(sid, body.title or "Untitled")


@app.delete("/api/sessions/{sid}")
def remove(sid: str):
    service.delete_session(sid)
    return {"deleted": sid}


@app.post("/api/sessions/{sid}/files")
def upload(sid: str, files: list[UploadFile] = File(...)):
    service.get_session(sid)
    return service.add_files(sid, [(f.filename or "upload", f.file.read()) for f in files])


@app.get("/api/sessions/{sid}/documents/{doc_id}/file")
def original(sid: str, doc_id: str):
    s = service.get_session(sid)
    doc = next((d for d in s["documents"] if d["id"] == doc_id), None)
    if not doc:
        raise HTTPException(404, "document not found")
    return FileResponse(store.file_path(sid, doc), filename=doc["filename"])


@app.post("/api/sessions/{sid}/documents/{doc_id}/reprocess")
def reprocess(sid: str, doc_id: str):
    return service.reprocess_document(sid, doc_id)


@app.get("/api/sessions/{sid}/record")
def record(sid: str, detail: str = "full"):
    rec = service.get_record(sid)
    if detail == "values":
        rec["record"] = simple_values(rec["record"])
    return rec


@app.get("/api/sessions/{sid}/conflicts")
def conflicts(sid: str):
    return service.get_record(sid)["conflicts"]


@app.post("/api/sessions/{sid}/reconcile")
def reconcile(sid: str):
    err = service.reconcile(sid)
    return {"error": err, "conflicts": service.get_record(sid)["conflicts"]}


@app.post("/api/sessions/{sid}/chat")
def chat(sid: str, body: ChatIn):
    service.get_session(sid)
    return chat_mod.chat(sid, body.message)


@app.post("/api/sessions/{sid}/ask")
def ask(sid: str, body: AskIn):
    return {"answer": service.ask_documents(sid, body.question, body.doc_ids)}


@app.post("/api/sessions/{sid}/custom-fields")
def custom_fields(sid: str, body: CustomFieldsIn):
    return service.add_custom_fields(sid, [f.model_dump() for f in body.fields])


@app.delete("/api/sessions/{sid}/custom-fields/{name}")
def delete_custom_field(sid: str, name: str):
    service.remove_custom_field(sid, name)
    return {"removed": name}


@app.post("/api/sessions/{sid}/overrides")
def override(sid: str, body: OverrideIn):
    return service.set_override(sid, body.path, body.value, body.note)


@app.post("/api/sessions/{sid}/publish")
def publish(sid: str, body: PublishIn | None = None):
    return service.publish(sid, body.note if body else None)


# ------------------------------------------------------------------------------ portal
@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


def main():
    import uvicorn

    uvicorn.run("app.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
