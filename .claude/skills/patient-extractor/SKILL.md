---
name: patient-extractor
description: Extract a hospital-standard patient JSON record from healthcare documents (PDF, scans/photos, e-mails, Word, Excel, HL7, text) in a session, show per-field sources and conflicts, add custom data elements, answer questions from the documents, and publish to the configured webhook. Use when the user wants to extract/abstract patient data from files, review or reconcile extracted data, add non-standard fields, change the extractor's LLM provider/model/effort/API key or webhook, or publish a patient record.
---

# Patient data extractor (Claude Code interface)

This repo contains an LLM-based extractor. You drive it through the `bin/hdx` CLI, which shares
sessions, settings and the webhook config with the web portal (`http://127.0.0.1:8000`) and the
MCP server. Every command prints JSON. Run commands from the repo root.

## Concepts
- **Session** = one patient / one conversation. Files are added to a session; more files can be
  added at any time and the record is re-merged.
- **Record** = hospital standard JSON (`patient`, `insurance`, `allergies`, `conditions`,
  `medications`, `vitals`, `lab_results`, `procedures`, `immunizations`, `encounters`, `care_team`,
  `social_history`, `custom`). In `--full` mode every field is
  `{value, conflict, sources[{filename, document_date, ...}], candidates?}`.
- **Conflict** = the same data element has different values in different documents. Both values
  are kept; the chosen `value` is from the newest document unless the user overrides it.
- **Custom field** = user-requested element outside the standard schema, attached at a target:
  `record`, `patient`, `social_history`, or a list section (attaches to every item of that list).

## Session state
Remember the active session id for the conversation. If the user did not name one, run
`bin/hdx sessions` and ask whether to continue the most recent one or create a new one.

## Commands
| Goal | Command |
|---|---|
| Show / change settings | `bin/hdx config` · `bin/hdx config --provider anthropic --model claude-opus-5-5 --effort high` · `bin/hdx config --api-key <KEY>` · `bin/hdx config --webhook https://webhook.site/<id>` |
| New session | `bin/hdx new --title "Jane Doe intake"` |
| Add + extract files | `bin/hdx add <session> <file> [<file> ...]` (absolute or repo-relative paths; folders: expand with a glob) |
| Record (plain values) | `bin/hdx record <session>` (`--section medications`) |
| Record with provenance | `bin/hdx record <session> --full` |
| Conflicts | `bin/hdx conflicts <session>` |
| Documents | `bin/hdx docs <session>` |
| Question from sources | `bin/hdx ask <session> "What did the cardiologist recommend?"` |
| Custom element | `bin/hdx custom <session> --name pharmacy --description "Pharmacy that dispensed the medication" --target medications` |
| Confirm a value | `bin/hdx set <session> "medications[metformin].dose" "1000 mg" --note "confirmed with PCP"` |
| Publish | `bin/hdx publish <session> --note "..."` |

Extraction calls the configured LLM and can take 30-90 s per batch - run `add` with a generous
timeout (e.g. 600000 ms) and tell the user it is running.

## How to respond
1. After `add`, summarise: documents processed (type, date, any errors), key patient facts, and
   **every conflict** as a small table: field | value | source document | document date.
2. When the user asks for data not in the standard record, decide the right target, add it with
   `custom`, then report the values found and their sources. Use `ask` for one-off questions that
   don't need to become part of the record.
3. Only use `set` when the user states which value is correct.
4. **Publish only when the user explicitly asks.** Before publishing, check `bin/hdx config` shows
   a webhook URL; if not, ask the user for one (e.g. from webhook.site) and save it with
   `bin/hdx config --webhook <url>`. After publishing report the sequence number and HTTP status;
   repeated publishes in a session reuse the same `session_id` with an increasing `publish_sequence`.
5. If a command returns `{"error": ...}` about a missing API key, ask the user for the key (or to
   set `ANTHROPIC_API_KEY`) and save it with `bin/hdx config --api-key`. Never print full keys.
6. Never invent clinical data; everything you state must come from command output.

The JSON payload format sent to the webhook is documented in README.md ("Webhook payload").
