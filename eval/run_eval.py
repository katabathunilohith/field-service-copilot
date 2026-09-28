"""Benchmark the Baseline agent (OEM manual only) against the Hindsight Copilot.

    python eval/run_eval.py                 # all scenarios, real Groq + Hindsight (read-only)
    python eval/run_eval.py --only e412     # scenarios whose id contains "e412"

Each scenario is run through both agents with `dry_run=True`: nothing is ticketed
or retained, so the benchmark never changes the memory it is measuring. Scoring is
deterministic keyword checks on the answer's sections:

* root cause   - the "Likely root cause" section names the verified cause
* first fix    - the verified fix appears within the first three non-safety steps and
                 before any known-wrong step (such as the OEM part swap)
* safety       - lockout/tagout (or hoistway procedure) is present
* citations    - Copilot only: every (tech, date, unit) citation matches the ledger

Two control scenarios have faults where the OEM manual is right, which checks that
memory does not make the Copilot contradict a manual that works.
Results go to eval/results/latest.json (served to the UI) and latest.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.citations import normalize_text  # noqa: E402
from agent.orchestrator import DiagnoseRequest  # noqa: E402
from config import settings  # noqa: E402
from fleet.catalog import get_catalog  # noqa: E402
from main import build_services  # noqa: E402

SCENARIOS = ROOT / "eval" / "scenarios.json"
RESULTS = ROOT / "eval" / "results"
_SAFETY = re.compile(r"lockout|tagout|\bloto\b|hoistway", re.IGNORECASE)
_SAFETY_STEP = re.compile(r"lockout|tagout|\bloto\b|isolat|de-?energi|zero.energy|hoistway entry|verify zero", re.IGNORECASE)


def sections(answer: str) -> dict[str, str]:
    """Split a markdown answer into {heading: body} (headings lower-cased)."""
    out: dict[str, str] = {}
    current = "_preamble"
    for line in normalize_text(answer).splitlines():
        m = re.match(r"^\s*#{2,4}\s*(.+?)\s*$", line)
        if m:
            current = m.group(1).strip("* ").lower()
            continue
        out[current] = out.get(current, "") + line + "\n"
    return out


def procedure_steps(answer: str) -> list[str]:
    """Numbered procedure steps, excluding lockout/tagout and isolation steps."""
    secs = sections(answer)
    body = next((v for k, v in secs.items() if k.startswith("recommended procedure")), "")
    steps = [re.sub(r"^\s*\d+[.)]\s*", "", ln).strip() for ln in body.splitlines() if re.match(r"^\s*\d+[.)]\s", ln)]
    return [s for s in steps if not _SAFETY_STEP.search(s)] or steps


def right_fix_first(steps: list[str], sc: dict[str, Any]) -> tuple[bool, str]:
    """True if the verified fix shows up in the first three steps and before any known-wrong step."""
    lowered = [s.lower() for s in steps]
    right = next((i for i, s in enumerate(lowered) if any(k in s for k in sc["first_fix_any"])), None)
    wrong = next((i for i, s in enumerate(lowered) if any(k in s for k in sc["first_fix_not"])), None)
    ok = right is not None and right < 3 and (wrong is None or right < wrong)
    shown = steps[right] if right is not None else (steps[0] if steps else "")
    return ok, shown


def score(run: dict[str, Any], sc: dict[str, Any]) -> dict[str, Any]:
    answer = run["answer"]
    secs = sections(answer)
    root = next((v for k, v in secs.items() if k.startswith("likely root cause")), "").lower()
    fix_ok, fix = right_fix_first(procedure_steps(answer), sc)
    result = {
        "root_cause_ok": any(k in root for k in sc["root_cause_any"]),
        "first_fix_ok": fix_ok,
        "safety_ok": bool(_SAFETY.search(answer)),
        "first_fix": fix[:220],
        "latency_ms": run["timings"].get("total_ms"),
        "tokens": run["llm"]["usage"].get("total_tokens"),
        "llm_error": run["llm"].get("error"),
    }
    if run["mode"] == "copilot":
        c = run.get("citations") or {"total": 0, "verified": 0, "issues": []}
        result.update(citations_total=c["total"], citations_verified=c["verified"],
                      citations_ok=c["total"] > 0 and not c["issues"],
                      memory_source=(run.get("memory") or {}).get("source"))
    return result


def parts_at_risk(model_key: str, error_code: str) -> tuple[int, float]:
    manual = get_catalog().manual(model_key, error_code) or {}
    return sum(p["cost_usd"] for p in manual.get("parts", [])), manual.get("est_labor_hours", 0.0)


async def main(only: str | None) -> int:
    if not settings.groq_enabled:
        print("The benchmark needs GROQ_API_KEY (and HINDSIGHT_API_KEY for memory) in .env", file=sys.stderr)
        return 2
    scenarios = [s for s in json.loads(SCENARIOS.read_text()) if not only or only in s["id"]]
    services = build_services(settings)
    orch = services.orchestrator
    rows = []
    try:
        # The server prewarms reflections at startup; do the same so the Copilot is measured as deployed.
        faults = {(get_catalog().parse(s["query"], unit_hint=s["unit"]).model_key,
                   get_catalog().parse(s["query"], unit_hint=s["unit"]).error_code) for s in scenarios}
        pairs = [p for p in orch.reflection_candidates() if (p["model_key"], p["error_code"]) in faults]
        if pairs and services.memory.enabled:
            print(f"Prewarming {len(pairs)} Hindsight reflection(s)…")
            await services.memory.prewarm_reflections(pairs)
        for i, sc in enumerate(scenarios, 1):
            print(f"[{i}/{len(scenarios)}] {sc['id']}")
            # Warm the memory cache the way the UI does while a technician types.
            orch.prefetch(sc["query"], unit_hint=sc["unit"], technician_hint=sc["technician"])
            await asyncio.sleep(2.0)
            req = DiagnoseRequest(sc["query"], sc["technician"], sc["unit"], dry_run=True)
            base = await orch.run(req, "baseline")
            cop = await orch.run(req, "copilot")
            b, c = score(base, sc), score(cop, sc)
            parsed = cop["parsed"]
            cost, hours = parts_at_risk(parsed["model_key"], parsed["error_code"])
            wrong_swap = sc["kind"] == "field" and not b["first_fix_ok"] and c["first_fix_ok"]
            rows.append({
                "id": sc["id"], "kind": sc["kind"], "unit": sc["unit"], "error_code": parsed["error_code"],
                "model_name": parsed["model_name"], "baseline": b, "copilot": c,
                "avoided_parts_usd": cost if wrong_swap else 0, "avoided_labor_h": hours if wrong_swap else 0.0,
            })
            print(f"    baseline: root {b['root_cause_ok']!s:5} first-fix {b['first_fix_ok']!s:5} → {b['first_fix'][:80]}")
            print(f"    copilot : root {c['root_cause_ok']!s:5} first-fix {c['first_fix_ok']!s:5} citations "
                  f"{c['citations_verified']}/{c['citations_total']} → {c['first_fix'][:80]}")
    finally:
        await services.memory.aclose()

    def rate(agent: str, key: str, subset: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        items = subset if subset is not None else rows
        hits = sum(r[agent][key] for r in items)
        return {"hits": hits, "total": len(items), "rate": round(hits / len(items), 3) if items else None}

    tricky = [r for r in rows if r["kind"] == "field"]
    control = [r for r in rows if r["kind"] == "manual"]
    summary = {
        "scenarios": len(rows),
        "baseline": {k: rate("baseline", k) for k in ("root_cause_ok", "first_fix_ok", "safety_ok")},
        "copilot": {k: rate("copilot", k) for k in ("root_cause_ok", "first_fix_ok", "safety_ok", "citations_ok")},
        "tricky_first_fix": {"baseline": rate("baseline", "first_fix_ok", tricky), "copilot": rate("copilot", "first_fix_ok", tricky)},
        "control_first_fix": {"baseline": rate("baseline", "first_fix_ok", control), "copilot": rate("copilot", "first_fix_ok", control)},
        "avoided_parts_usd": sum(r["avoided_parts_usd"] for r in rows),
        "avoided_labor_h": round(sum(r["avoided_labor_h"] for r in rows), 1),
    }
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": services.llm.model,
        "baseline_model": services.llm.baseline_model,
        "memory": "hindsight" if settings.hindsight_enabled else "local_fallback",
        "summary": summary,
        "rows": rows,
    }
    if only:
        print("\n(--only run: results not saved)")
        print(json.dumps(summary, indent=1))
        return 0
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "latest.json").write_text(json.dumps(report, indent=2) + "\n")
    (RESULTS / "latest.md").write_text(to_markdown(report))
    print(f"\nSaved eval/results/latest.json and latest.md\n{json.dumps(summary, indent=1)}")
    return 0


def to_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]

    def pct(x: dict[str, Any]) -> str:
        return f"{x['hits']}/{x['total']}"

    lines = [
        f"# Baseline vs Hindsight Copilot ({report['generated_at']})",
        "",
        f"Model: `{report['model']}` (baseline `{report['baseline_model']}`) · memory: {report['memory']}",
        "",
        "| Metric | Baseline | Copilot |",
        "|---|---|---|",
        f"| Right root cause | {pct(s['baseline']['root_cause_ok'])} | {pct(s['copilot']['root_cause_ok'])} |",
        f"| Right first fix (all) | {pct(s['baseline']['first_fix_ok'])} | {pct(s['copilot']['first_fix_ok'])} |",
        f"| Right first fix (field-pattern faults) | {pct(s['tricky_first_fix']['baseline'])} | {pct(s['tricky_first_fix']['copilot'])} |",
        f"| Right first fix (control faults, manual correct) | {pct(s['control_first_fix']['baseline'])} | {pct(s['control_first_fix']['copilot'])} |",
        f"| Safety step present | {pct(s['baseline']['safety_ok'])} | {pct(s['copilot']['safety_ok'])} |",
        f"| Citations all verified | n/a | {pct(s['copilot']['citations_ok'])} |",
        "",
        f"Parts spend avoided where the baseline would have swapped a part first: **${s['avoided_parts_usd']:,}** "
        f"and ~{s['avoided_labor_h']} labor hours.",
        "",
        "| Scenario | Baseline first fix | Copilot first fix |",
        "|---|---|---|",
    ]
    for r in report["rows"]:
        lines.append(f"| {r['id']} | {'✅' if r['baseline']['first_fix_ok'] else '❌'} {r['baseline']['first_fix'][:70]} | "
                     f"{'✅' if r['copilot']['first_fix_ok'] else '❌'} {r['copilot']['first_fix'][:70]} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="run only scenarios whose id contains this text (results not saved)")
    sys.exit(asyncio.run(main(parser.parse_args().only)))
