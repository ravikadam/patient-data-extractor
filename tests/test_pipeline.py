"""Offline pipeline test: a fake LLM returns canned extractions so merge/conflicts/custom/publish can be checked
without API calls.  Run: .venv/bin/python -m pytest -q tests  (or python tests/test_pipeline.py)"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp()
os.environ["HDX_DATA_DIR"] = _tmp
os.environ["HDX_CONFIG_DIR"] = _tmp

from app import service  # noqa: E402
from app.config import update_settings  # noqa: E402
from app.llm.base import LLMProvider  # noqa: E402

CANNED = {
    "discharge": {"document": {"document_type": "discharge summary", "document_date": "2026-08-14"},
                  "patient": {"name": {"first": "Eleanor", "last": "Vasquez"}, "date_of_birth": "1957-06-22",
                              "phone": {"mobile": "(217) 555-0148"}, "mrn": "RMC-0048213"},
                  "medications": [{"name": "metformin", "dose": "500 mg", "frequency": "twice daily"},
                                  {"name": "Lisinopril", "dose": "10 mg"}],
                  "allergies": [{"substance": "Penicillin", "reaction": "hives"}],
                  "lab_results": [{"test": "Hemoglobin A1c", "value": "8.4", "date": "2026-08-11"}]},
    "email": {"document": {"document_type": "email", "document_date": "2026-09-03"},
              "patient": {"date_of_birth": "06/22/1957", "phone": {"mobile": "217-555-0177"}},
              "medications": [{"name": "Metformin HCl", "dose": "1000mg", "frequency": "twice daily"},
                              {"name": "lisinopril", "status": "discontinued"}],
              "allergies": [{"substance": "penicillins", "reaction": "lip swelling"}, {"substance": "sulfa", "reaction": "rash"}],
              "lab_results": [{"test": "HbA1c", "value": "7.9", "date": "2026-09-01"}]},
}
CUSTOM = {"email": {"medications": [{"name": "metformin", "custom": {"pharmacy": "Walgreens #4471"}}],
                    "custom": {"code_status": "Full code"}}}


class FakeLLM(LLMProvider):
    def __init__(self):
        super().__init__("k", "fake", "low", 1000)

    def generate(self, system, parts, max_tokens=None):
        header = parts[0]["text"]
        key = "email" if ".eml" in header or "email" in header else "discharge"
        is_custom = "ONLY these additional" in parts[-1]["text"]
        return json.dumps(CUSTOM.get(key, {}) if is_custom else CANNED[key])


service.get_llm = lambda: FakeLLM()  # type: ignore


class Hook(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        Hook.received.append((dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def test_pipeline():
    srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    update_settings({"webhook": {"url": f"http://127.0.0.1:{srv.server_port}/hook"}})

    sid = service.create_session("t")["id"]
    service.add_files(sid, [("discharge.pdf", b"%PDF-1.4 fake")])
    rec = service.get_record(sid)
    assert rec["conflicts"] == []
    service.add_files(sid, [("note.txt", b"email text")])  # header contains filename only -> rename below
    rec = service.get_record(sid)
    # second file maps to "discharge" again (same values) -> still no conflicts; now add a real email
    eml = b"From: a@b\nTo: c@d\nDate: Thu, 03 Sep 2026 09:14:00 -0500\nSubject: x\n\nbody"
    service.add_files(sid, [("referral.eml", eml)])
    rec = service.get_record(sid)
    paths = {c["path"] for c in rec["conflicts"]}
    assert "patient.phone.mobile" in paths, paths
    assert "medications[metformin].dose" in paths, paths            # 500 mg vs 1000mg, HCl normalised away
    assert "allergies[penicillin].reaction" in paths, paths          # penicillins ~ penicillin
    assert "patient.date_of_birth" not in paths                      # 1957-06-22 == 06/22/1957
    med = next(m for m in rec["record"]["medications"] if m["entity_key"] == "metformin")
    assert med["dose"]["value"] == "1000mg"                           # newest document wins
    assert {s["filename"] for s in med["dose"]["sources"]} == {"discharge.pdf", "note.txt", "referral.eml"}
    labs = {l["entity_key"] for l in rec["record"]["lab_results"]}
    assert labs == {"hba1c|2026-08-11", "hba1c|2026-09-01"}, labs

    out = service.add_custom_fields(sid, [{"name": "pharmacy", "description": "dispensing pharmacy", "target": "medications"},
                                          {"name": "code_status", "description": "code status", "target": "record"}])
    assert out["values"]["medications[metformin].custom.pharmacy"]["value"] == "Walgreens #4471", out
    assert out["values"]["custom.code_status"]["value"] == "Full code"

    service.set_override(sid, "medications[metformin].dose", "1000 mg", "confirmed")
    med = next(m for m in service.get_record(sid)["record"]["medications"] if m["entity_key"] == "metformin")
    assert med["dose"]["value"] == "1000 mg" and med["dose"]["candidates"][0]["sources"][0]["doc_id"] == "user"

    from app import store
    with store.mutate(sid) as s:
        s["entity_aliases"] = {"allergies": {"sulfa": "penicillin"}}  # deliberately odd, just to test aliasing
    keys = [a["entity_key"] for a in service.get_record(sid)["record"]["allergies"]]
    assert keys == ["penicillin"], keys
    with store.mutate(sid) as s:
        s["entity_aliases"] = {}

    p1 = service.publish(sid, "first"); p2 = service.publish(sid, "second")
    assert p1["ok"] and p2["ok"] and p2["sequence"] == 2
    (h1, b1), (h2, b2) = Hook.received[-2:]
    assert b1["session_id"] == b2["session_id"] == sid and h2["X-HDX-Session-Id"] == sid
    assert b2["publish_sequence"] == 2 and b2["is_update"] and b1["publish_id"] != b2["publish_id"]
    assert b2["record_values"]["medications"][0]["entity_key"]
    srv.shutdown()
    print("pipeline OK:", len(rec["conflicts"]), "conflicts;", sorted(paths))


if __name__ == "__main__":
    test_pipeline()
