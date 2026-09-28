"""Fleet learning-curve metrics, computed from the service history (never hardcoded).

First-time-fix (FTF): a work order whose first visit's fix held with no callback.
Memory density: cumulative number of retained repair records at the end of each week.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fleet.catalog import Catalog


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def fleet_metrics(seed: dict[str, Any], catalog: Catalog, tickets: list[dict[str, Any]], runtime_records: int) -> dict[str, Any]:
    work_orders = seed["work_orders"]
    weeks_meta = seed["weeks"]
    visits = [(wo, v) for wo in work_orders for v in wo["visits"]]

    weeks = []
    for meta in weeks_meta:
        wos = [wo for wo in work_orders if wo["week"] == meta["week"]]
        ftf = sum(wo["first_time_fix"] for wo in wos)
        first_visits = [wo["visits"][0] for wo in wos]
        memories = sum(1 for _, v in visits if v["timestamp"][:10] <= meta["end"])
        weeks.append({
            "week": meta["week"],
            "label": meta["label"],
            "start": meta["start"],
            "end": meta["end"],
            "jobs": len(wos),
            "first_time_fixes": ftf,
            "ftf_rate": _rate(ftf, len(wos)),
            "callbacks": sum(len(wo["visits"]) - 1 for wo in wos),
            "memory_records": memories,
            "peer_assisted_jobs": sum(v["knowledge_source"] == "peer_memory" for v in first_visits),
            "field_fix_first_visits": sum(v["action_category"] == "field" for v in first_visits),
            "labor_hours": round(sum(wo["total_labor_hours"] for wo in wos), 1),
            "parts_cost_usd": sum(wo["total_parts_cost_usd"] for wo in wos),
        })

    # Peer learning ledger: who discovered what, and who later benefited.
    transfers = []
    for disc in seed.get("discoveries", []):
        code = disc["knowledge_key"].replace("-alt", "")
        reuses = [
            {
                "technician": v["technician"],
                "date": v["timestamp"][:10],
                "unit_id": wo["unit_id"],
                "site": wo["site"],
                "work_order": wo["id"],
                "held": v["outcome_held"],
                "peer": v["knowledge_source"] == "peer_memory",
            }
            for wo, v in visits
            if v.get("learned_from") and v["learned_from"]["work_order"] == disc["work_order"]
        ]
        disc_visit = next(
            v for wo, v in visits if wo["id"] == disc["work_order"] and v["knowledge_source"] == "discovery"
        )
        wo = next(w for w in work_orders if w["id"] == disc["work_order"])
        held = sum(r["held"] for r in reuses)
        transfers.append({
            "knowledge_key": disc["knowledge_key"],
            "error_code": code,
            "model_name": wo["model_name"],
            "finding": disc_visit["root_cause"],
            "fix": disc_visit["action"],
            "discovered_by": disc["technician"],
            "discovered_role": catalog.technician_role(disc["technician"]),
            "discovered_on": disc["date"],
            "discovered_unit": disc["unit_id"],
            "reuses": reuses,
            "reuse_count": len(reuses),
            "reuse_held": held,
            "beneficiaries": sorted({r["technician"] for r in reuses if r["peer"]}),
        })

    # Per-technician first-visit performance, early vs late in the period.
    per_tech: dict[str, dict[str, list[bool]]] = defaultdict(lambda: {"early": [], "late": []})
    for wo in work_orders:
        tech = wo["visits"][0]["technician"]
        per_tech[tech]["early" if wo["week"] <= 2 else "late"].append(wo["first_time_fix"])
    technicians = [
        {
            "technician": tech,
            "role": catalog.technician_role(tech),
            "jobs": len(buckets["early"]) + len(buckets["late"]),
            "ftf_weeks_1_2": _rate(sum(buckets["early"]), len(buckets["early"])),
            "ftf_weeks_3_4": _rate(sum(buckets["late"]), len(buckets["late"])),
        }
        for tech, buckets in sorted(per_tech.items())
    ]

    first, last = weeks[0], weeks[-1]
    baseline_callback_rate = first["callbacks"] / first["jobs"] if first["jobs"] else 0
    confirmed = [t for t in tickets if t.get("outcome_held") is not None]
    return {
        "weeks": weeks,
        "summary": {
            "ftf_start": first["ftf_rate"],
            "ftf_end": last["ftf_rate"],
            "ftf_delta_pts": round((last["ftf_rate"] - first["ftf_rate"]) * 100, 1),
            "total_jobs": len(work_orders),
            "memory_records_seeded": len(visits),
            "memory_records_live": runtime_records,
            "callbacks_avoided_last_week": round(baseline_callback_rate * last["jobs"] - last["callbacks"], 1),
            "field_discoveries": len(transfers),
            "peer_reuses": sum(sum(r["peer"] for r in t["reuses"]) for t in transfers),
        },
        "knowledge_transfers": transfers,
        "technicians": technicians,
        "live": {
            "tickets_open": sum(t.get("outcome_held") is None for t in tickets),
            "tickets_confirmed": len(confirmed),
            "tickets_held": sum(bool(t["outcome_held"]) for t in confirmed),
        },
        "definition": "First-time-fix = work orders whose first visit's fix held with no callback, divided by all work orders opened that week.",
    }
