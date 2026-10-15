"""`hdx` command line - used by the Claude Code skill, scripts and humans.

All commands print JSON to stdout. Sessions/settings are shared with the web portal and MCP server.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import chat as chat_mod
from . import service
from .config import public_settings, update_settings
from .llm import LLMError
from .merge import simple_values


def out(x) -> None:
    print(json.dumps(x, indent=1, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hdx", description="Healthcare document -> patient JSON extractor")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", help="show or change settings")
    c.add_argument("--provider"); c.add_argument("--model"); c.add_argument("--effort")
    c.add_argument("--api-key", help="API key for the (new) provider"); c.add_argument("--max-tokens", type=int)
    c.add_argument("--webhook", help="publish webhook URL")

    sub.add_parser("sessions", help="list sessions")
    n = sub.add_parser("new", help="create session"); n.add_argument("--title")
    a = sub.add_parser("add", help="add files to a session and extract"); a.add_argument("session"); a.add_argument("files", nargs="+")
    r = sub.add_parser("record", help="print merged record"); r.add_argument("session")
    r.add_argument("--full", action="store_true", help="include provenance/conflict candidates"); r.add_argument("--section")
    sub.add_parser("conflicts", help="list conflicts").add_argument("session")
    sub.add_parser("docs", help="list documents").add_argument("session")
    sub.add_parser("reconcile", help="re-run LLM equivalence check on differing values").add_argument("session")
    q = sub.add_parser("ask", help="question answered from the source documents"); q.add_argument("session"); q.add_argument("question")
    cf = sub.add_parser("custom", help="add a custom data element")
    cf.add_argument("session"); cf.add_argument("--name", required=True); cf.add_argument("--description", required=True)
    cf.add_argument("--target", default="record"); cf.add_argument("--type", default="string")
    o = sub.add_parser("set", help="set a user-confirmed field value"); o.add_argument("session"); o.add_argument("path"); o.add_argument("value"); o.add_argument("--note")
    ch = sub.add_parser("chat", help="talk to the session chat agent"); ch.add_argument("session"); ch.add_argument("message")
    pb = sub.add_parser("publish", help="send record to webhook"); pb.add_argument("session"); pb.add_argument("--note")

    args = p.parse_args(argv)
    try:
        if args.cmd == "config":
            patch: dict = {"llm": {}, "webhook": {}}
            for k in ("provider", "model", "effort", "max_tokens"):
                if getattr(args, k) is not None:
                    patch["llm"][k] = getattr(args, k)
            if args.api_key:
                prov = args.provider or public_settings()["llm"]["provider"]
                patch["llm"]["api_keys"] = {prov: args.api_key}
            if args.webhook is not None:
                patch["webhook"]["url"] = args.webhook
            if patch["llm"] or patch["webhook"]:
                update_settings(patch)
            s = public_settings()
            out({"llm": {k: s["llm"][k] for k in ("provider", "model", "effort", "max_tokens", "api_keys", "api_key_status")},
                 "webhook": s["webhook"], "settings_path": s["settings_path"]})
        elif args.cmd == "sessions":
            out(service.list_sessions())
        elif args.cmd == "new":
            s = service.create_session(args.title); out({"session_id": s["id"], "title": s["title"]})
        elif args.cmd == "add":
            res = service.add_file_paths(args.session, args.files)
            out({"documents": [{k: d.get(k) for k in ("id", "filename", "status", "document_type", "document_date", "error")}
                               for d in res["documents"]], "conflicts": res["conflicts"]})
        elif args.cmd == "record":
            rec = service.get_record(args.session)
            data = rec["record"] if args.full else simple_values(rec["record"])
            if args.section:
                data = {args.section: data.get(args.section)}
            out({"session_id": args.session, "record": data, "conflict_count": len(rec["conflicts"])})
        elif args.cmd == "conflicts":
            out(service.get_record(args.session)["conflicts"])
        elif args.cmd == "reconcile":
            err = service.reconcile(args.session)
            out({"error": err, "conflicts": service.get_record(args.session)["conflicts"]})
        elif args.cmd == "docs":
            out(service.get_record(args.session)["documents"])
        elif args.cmd == "ask":
            print(service.ask_documents(args.session, args.question))
        elif args.cmd == "custom":
            out(service.add_custom_fields(args.session, [{"name": args.name, "description": args.description,
                                                         "target": args.target, "type": args.type}]))
        elif args.cmd == "set":
            out(service.set_override(args.session, args.path, args.value, args.note))
        elif args.cmd == "chat":
            print(chat_mod.chat(args.session, args.message)["content"])
        elif args.cmd == "publish":
            out(service.publish(args.session, args.note))
    except (KeyError, ValueError, FileNotFoundError, LLMError) as e:
        out({"error": str(e).strip("'\"")})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
