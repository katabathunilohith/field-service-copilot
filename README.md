# Field Service Copilot

A diagnostic copilot for technicians repairing commercial HVAC chillers, solar inverters and elevator hoists. Its value isn't chat. It's **learning that persists across sessions and across technicians**, backed by [Hindsight](https://hindsight.vectorize.io) long-term memory.

> **The story the seeded data tells.** On 1 Sept, junior tech Alex follows the OEM manual for a Carrier 30XA `E-412` and replaces a $4,850 inverter board. The fault comes back. On 3 Sept, senior tech Dave finds the real cause: a loose, vibration-chafed J4 harness. That finding is retained. On 8 Sept, Alex hits `E-412` on a different chiller. The Copilot recalls Dave's finding, Alex checks the harness first, and it holds. When Alex confirms the outcome, the hold count goes up for Sarah's next session. Across the fleet, first-time-fix climbs from **61% in week 1 to 94% in week 4**, computed from the work orders rather than hardcoded.

## What's in the box

| Area | What it does |
|---|---|
| **Diagnostic console** | Ask about a unit and fault code. The *Baseline agent* (OEM manual + telemetry, no memory) and the *Hindsight Copilot* answer side by side from the same LLM and tools. |
| **Memory delta** | Each Copilot answer shows what memory changed versus the manual, e.g. *“OEM manual recommends replacing the board, but Tech_Dave's harness fix held on 7 of 7 occurrences… Exception: 1 time the harness was fine.”* Site-aware: at a desert site it surfaces the MC4-connector variant of `GF-17`, not the coastal grounding-strap fix. |
| **Closed learning loop** | Every Copilot session opens a ticket and is retained. **Fix held / Didn't hold** retains the outcome, and the next technician's statistics move. |
| **Memory Inspector** | A slide-over with recalled memories (score, date, technician, outcome), the retain event log with raw payloads, reflection synthesis (auto-triggered or on demand), and every raw request/response. |
| **Fleet learning** | First-time-fix by week, memory density, a peer-learning ledger (who discovered what and who later benefited), and per-technician first-time-fix. |
| **Field notes & "Before you go" briefing** | Tribal knowledge that isn't a repair outcome: site access rules, hazards and equipment quirks ("Tower B roof needs an escort after 6pm", "bring the long MC4 tool"). Technicians type or speak a note; it is retained to Hindsight, scoped to the unit or the whole site. Selecting a unit shows every note for it and its site, credited and dated, before a question is asked, and the Copilot opens its procedure with the relevant hazards and rules. Access codes, passwords, phone numbers and emails are redacted before anything is saved. |

## Quick start

Prerequisites: Python 3.10+ and Node 20+.

```bash
cp .env.example .env
```

Fill in `HINDSIGHT_API_KEY` and `GROQ_API_KEY` in `.env`. The file is git-ignored. Everything also runs **without keys** (see [Graceful degradation](#graceful-degradation)).

```bash
python3 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
npm install
```

Seed the Hindsight bank with the 4-week service history (88 repair records and 7 field notes). Re-running is idempotent; `--notes-only` sends just the field notes.

```bash
python data/seeder.py
```

Run the API (port 8000) and, in a second terminal, the UI (port 5173):

```bash
python main.py
```

```bash
npm run dev
```

Open http://localhost:5173. Pick the **Alex · CHL-0417** example and press **Diagnose**.

Run the end-to-end workflow check (no browser, no keys needed):

```bash
python test_workflow.py
```

With real keys, `python test_workflow.py --live` runs the same flow against Groq and Hindsight.

## Configuration: where keys are bound

| Setting | Read in | Notes |
|---|---|---|
| `HINDSIGHT_API_KEY`, `HINDSIGHT_BASE_URL` | `config.py` → `memory/hindsight_wrapper.py` (`HindsightMemory.__init__`) | Sent only as `Authorization: Bearer …`; never logged or shown in the inspector |
| `HINDSIGHT_BANK_ID` (default `field-service-copilot`) | `config.py`, `memory/bank_schemas.resolve_bank_id`, `data/seeder.py` (`BANK_ID`) | Seeder and backend resolve the bank the same way |
| `HINDSIGHT_PER_FLEET_BANKS` | `memory/bank_schemas.py` | `true` splits into `<bank>-chillers`, `<bank>-solar`, `<bank>-elevators`; every wrapper call takes `bank_id` |
| `HINDSIGHT_TIMEOUT_S` (1.5) / `HINDSIGHT_REFLECT_TIMEOUT_S` (12) | `config.py` | Interactive recall/retain budget; reflect runs an LLM on the Hindsight side, so it gets its own budget |
| `GROQ_API_KEY`, `GROQ_BASE_URL`, `GROQ_MODEL` | `config.py` → `agent/groq_client.py` | Default `openai/gpt-oss-120b` at `https://api.groq.com/openai/v1`; any Groq tool-calling model works (e.g. `qwen/qwen3-32b`) |
| `GROQ_REASONING_EFFORT`, `GROQ_MAX_RETRIES` | `agent/groq_client.py` | Reasoning effort is only sent to `openai/gpt-oss-*` models |

Placeholder values from `.env.example` (anything starting with `your_`) are treated as unset.

## Architecture

```
            ┌───────────────────────── ui/ (Vite + React + Tailwind) ─────────────────────────┐
            │  ChatWindow (baseline | copilot)   MemoryInspector (slide-over)   LearningMetrics │
            └──────────────────────────────────────┬──────────────────────────────────────────┘
                                                   │ /api (FastAPI, main.py)
                                   ┌───────────────▼────────────────┐
                                   │  agent/orchestrator.py          │
  question ─► parse unit/model/code ─► RECALL (fleet · unit · outcomes, concurrent, 1.5 s)
                                   │   └► memory delta (hold-rate stats, site-aware)
                                   │   └► REFLECT when memory contradicts the manual
                                   │  LLM tool loop (agent/groq_client.py + agent/tools.py)
                                   │   fetch_unit_telemetry · lookup_service_manual
                                   │   verify_fix_outcome · log_repair_ticket
                                   │  RETAIN diagnosis ─► ticket ─► RETAIN confirmed outcome
                                   └───────┬───────────────────────┬─┘
                  memory/hindsight_wrapper.py                fleet/ (catalog, tickets, metrics)
                  ├─ Hindsight REST  /v1/default/banks/{bank}/memories[/recall] · /reflect
                  └─ memory/fallback_store.py  (BM25 over seed + write-through journal)
```

### Memory design (`memory/bank_schemas.py`)

Every retained record is a `RepairRecord` with three record types. *Repair* records are technician visits and are phrased as world facts. *Diagnosis* records are the Copilot's own sessions, written in first person so they land as experience. *Outcome* records are field confirmations. Each retain item carries:

* **Tags** for scoped recall: `model:carrier-30xa`, `error:e-412`, `unit:chl-0417`, `tech:tech-dave`, `site:…`, `fleet:…`, `procedure:manual|field`, `outcome:held|failed|pending`, `record:repair|diagnosis|outcome`
* **Metadata** (string map) with the structured fields that drive the hold-rate statistics: technician, unit, action taken, action category, root cause, outcome, work order, and so on
* **Entities** typed as `technician`, `equipment_unit`, `equipment_model`, `error_code` and `site`, for Hindsight's entity graph
* A stable **`document_id`** (`wo-2026-0004-v2`, `tkt-…-outcome`), so re-seeding replaces records instead of duplicating them

The bank profile (`PUT /v1/default/banks/{id}`) sets a mission, background, retain mission and reflect mission tuned for field diagnostics.

### The diagnostic loop (`agent/orchestrator.py`)

1. **Parse** the unit, model and error code from free text, falling back to the UI's unit and technician selectors.
2. **Recall** three things concurrently under the 1.5 s budget: fleet history for this model and error, this unit's history, and outcome-bearing records for hold-rate statistics.
3. **Memory delta**: compute how often the OEM step and each field fix held, who first confirmed the fix and when, and where a known pattern was ruled out. Fixes that held at the technician's own site rank first.
4. **Reflect** only when memory contradicts the manual (OEM steps failed at least twice, or a field fix beat the manual at least three times). Results are cached for 10 minutes and invalidated on new retains.
5. **LLM tool loop** (up to 6 rounds). The Copilot gets `verify_fix_outcome` and `log_repair_ticket`; the baseline only gets the manual and telemetry.
6. **Retain** the session, pending confirmation. The technician's **Fix held / Didn't hold** answer retains the outcome. Free-text fixes are mapped onto the canonical ledger action, so hold counts accumulate on the same entry.

### Groq hardening (`agent/groq_client.py`)

* Exponential backoff with full jitter on 429, 5xx and connection errors, honouring `Retry-After`. SDK retries are disabled so one policy owns it.
* `repair_json`: handles Python-dict syntax, single quotes, trailing commas, code fences, double-encoded strings, over-escaped quotes, unquoted keys, and truncated objects (balanced closers).
* Groq `tool_use_failed` (400): the call is recovered from `failed_generation`. If that fails, it re-prompts once at temperature 0.
* Tool calls written into `content` (`<tool_call>…</tool_call>`, `<function=…>`, bare JSON) are parsed out.
* Mangled tool names (`functions.x<|channel|>commentary`, typos) are normalised and fuzzy-matched.
* Tool errors, including unrepairable arguments, go back to the model as tool results instead of crashing the loop.

### Graceful degradation

| Failure | Behaviour |
|---|---|
| Hindsight recall > 1.5 s or error | Local BM25 fallback over the seed plus the journal of everything retained; the inspector marks `local fallback · timeout` |
| Hindsight retain > 1.5 s or error | Record is journaled locally, the event is marked `deferred`, and 3 background retries follow |
| Hindsight reflect > 12 s or error | Deterministic synthesis from the same outcome statistics |
| Hindsight facts come back without structured metadata | Hold-rate stats use the write-through ledger, labelled `stats: local ledger` |
| Groq unavailable or no key | A deterministic brief from the manual and memory statistics, clearly labelled |
| No keys at all | The whole app still works on local data; the no-keys check in `test_workflow.py` proves it |

## Data (`data/`)

* `equipment_catalog.json` holds three models, eight error codes with OEM manual sections, 24 units across seven sites, telemetry baselines, and live anomaly profiles for the demo units.
* `field_notes_seed.json` has 7 technicians' field notes across four sites. Riverside Medical has none, so the demo can start from an empty briefing.
* `seed_history.json` has 72 work orders (88 technician visits) from 31 Aug to 27 Sept 2026. It is generated deterministically by `python data/generate_seed.py`, where the schedule is scripted and readable.
* It includes honest misses: a genuine board failure where the harness was fine, a site-dependent `GF-17` variant, a senior tech who skipped the Copilot, and ordinary control-case failures.

All data is synthetic. Model names are used for realism; error codes, part numbers and manual sections are illustrative and are not OEM documentation.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Configuration flags (no key material), Hindsight reachability, LLM availability |
| `GET` | `/api/catalog` | Technicians, models, error codes, units, example prompts |
| `POST` | `/api/diagnose` | `{query, technician_id, unit_id?, mode: baseline\|copilot\|compare, history}` |
| `POST` | `/api/tickets/{id}/outcome` | `{outcome_held, notes?, technician_id?}`; retains the outcome |
| `GET` | `/api/tickets` | Tickets opened by Copilot sessions |
| `GET` | `/api/memory/events` | Memory operation log for the inspector (`?op=recall\|retain\|reflect`) |
| `POST` | `/api/memory/reflect` | `{model, error_code, force}`; on-demand reflection |
| `GET` | `/api/metrics/fleet` | Weekly first-time-fix, memory density, peer-learning ledger, per-technician stats |
| `POST` | `/api/notes` | `{text, technician_id, unit_id, scope: unit\|site, kind?: site_rule\|hazard\|machine_quirk}`; screens, then retains a field note |
| `GET` | `/api/briefing?unit_id=` | Pre-visit briefing: field notes for the unit and its site, recalled from Hindsight |

`npm run build` writes `ui/dist`; when that folder exists, `python main.py` also serves the UI at http://localhost:8000.

## Project layout

```
config.py                  env-driven settings (no secrets in code)
main.py                    FastAPI app + static UI serving
memory/                    bank_schemas · hindsight_wrapper · fallback_store · insights · event_log
agent/                     groq_client · tools · prompts · orchestrator
fleet/                     catalog (manuals, telemetry, parsing) · tickets · metrics
data/                      equipment_catalog.json · seed_history.json · generate_seed.py · seeder.py
ui/src/                    App.tsx · components/{ChatWindow,MemoryInspector,LearningMetrics,ui}.tsx
test_workflow.py           end-to-end check (offline by default, --live for real services)
```

Runtime state (tickets and the retain journal) is written to `data/runtime/`, which is git-ignored. Delete it to reset live sessions.

## Demo: tribal knowledge in 60 seconds

1. As **Tech_Alex**, select **CHL-0417**. The "Before you go" strip is empty: nobody has noted anything about Riverside Medical.
2. **+ Add field note** → *Site rule*, *Whole site*: "Tower B roof needs a facilities escort after 6pm." (or tap the mic). Add an *Equipment quirk* for this unit: "VFD cabinet hinge is seized; bring a 10 mm socket."
3. Switch the technician to **Tech_Sarah** and select **ELV-0302**, a lift at the same hospital: the escort rule is already there, credited to Tech_Alex. Select **CHL-0417**: both notes.
4. Ask the E-412 question as Tech_Sarah. The pipeline shows "Site briefing: 2 field notes", and the answer opens with *Before you start*, crediting Tech_Alex.
5. For contrast, select **INV-3112** at Mesa Ridge: Tech_Dave's heat hazard and Tech_Sarah's MC4-tool quirk, both from the seeded weeks.
