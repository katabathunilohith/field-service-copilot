"""Deterministically generate data/seed_history.json.

The service history covers four weekly service cycles (2026-08-31 → 2026-09-27)
across three technicians and three equipment families. The schedule below is
scripted so the story is explicit and reproducible:

* Week 1: techs follow the OEM manual. Tricky faults keep coming back, and two
  field discoveries are made late in the week (E-412 harness, GF-17 strap).
* Week 2: three more discoveries; peers start applying earlier findings that the
  Copilot recalls from memory.
* Weeks 3-4: most tricky faults are fixed first time from peer memory. The
  remaining misses are honest: a genuine board failure, a site-dependent variant
  of GF-17, and ordinary control-case failures.

First-time-fix is *computed* from these work orders by fleet/metrics.py; it is
never hardcoded. Run:  python data/generate_seed.py
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = json.loads((HERE / "equipment_catalog.json").read_text())
OUT = HERE / "seed_history.json"

START = date(2026, 8, 31)
YEAR = 2026
TECHS = {"D": "Tech_Dave", "S": "Tech_Sarah", "A": "Tech_Alex"}
TECH_INFO = {t["id"]: t for t in CATALOG["technicians"]}

CODE_MODEL = {
    code: model_key
    for model_key, model in CATALOG["models"].items()
    for code in model["error_codes"]
}

# Faults where the OEM remedy usually misses the real root cause.
TRICKY = {"E-412", "E-221", "GF-17", "OT-05", "F-082"}

FIELD = {
    "E-412": {
        "action": "Re-terminate VFD J4 power harness with new ferrules (2.5 N·m) and fit a strain-relief bracket",
        "root_cause": "Loose, vibration-chafed J4 power harness at the VFD terminal block causing current spikes at compressor start",
        "short": "loose, vibration-chafed J4 harness at the VFD terminal block",
        "hours": 1.5, "cost": 35,
        "clue": "Current spikes lined up with compressor start-up vibration and the board had no stored IGBT fault.",
        "discovery": "found the J4 power harness loose at the VFD terminal block, insulation chafed where it rubs the compressor mounting rail; a wiggle test reproduced the overcurrent trip",
        "check": "J4 harness terminals were finger-loose with visible chafing",
        "tip": "On E-412, inspect and torque the VFD J4 harness before condemning the inverter board.",
        "genuine": "Megger 780 MΩ and supply imbalance 0.6% were within spec, so replaced inverter drive board 30XA-VFD-412B. The removed board had a shorted IGBT on phase W: a genuine board failure.",
        "genuine_cause": "Genuine inverter board failure (shorted IGBT, phase W)",
    },
    "E-221": {
        "action": "Re-seat LWT thermistor in its thermowell with fresh thermal compound and zero the offset in Service › Sensor Cal",
        "root_cause": "Dried thermal compound left an air gap in the LWT thermowell, biasing the reading +1.5 to +2.0 °C",
        "short": "air gap in the LWT thermowell from dried thermal compound",
        "hours": 0.75, "cost": 12,
        "clue": "The new thermistor read the same +1.8 °C high as the old one.",
        "discovery": "saw the replacement thermistor carried the identical +1.8 °C offset, pulled it and found the thermowell bone-dry with no thermal compound. Resistance matched the NTC chart for the reading, so the sensor element was healthy",
        "check": "the thermowell compound was dry and the probe sat loose",
        "tip": "If a new thermistor shows the same E-221 offset, the sensor is fine: re-seat it with thermal compound instead of ordering parts.",
        "genuine": "Thermistor went intermittently open-circuit on the resistance test, so replaced 30XA-THM-10K. Reading within 0.3 °C of the reference probe.",
        "genuine_cause": "Failed thermistor element (intermittent open circuit)",
    },
    "GF-17": {
        "action": "Replace corroded chassis grounding strap with tinned copper, clean the bond to bright metal, apply anti-oxidant and verify bond < 0.1 Ω",
        "root_cause": "Salt-fog corrosion on the chassis-to-racking grounding strap raised bond resistance and tripped the isolation monitor",
        "short": "salt-fog corrosion on the chassis grounding strap",
        "hours": 1.25, "cost": 45,
        "clue": "The fault tracked morning humidity and every string tested above 1 MΩ.",
        "discovery": "measured the chassis-to-racking bond at 2.7 Ω (spec < 0.1 Ω) and found the strap lug green with salt corrosion under its boot",
        "check": "the grounding strap bond measured 1.9 Ω with corrosion under the lug",
        "tip": "At coastal sites, measure the chassis grounding strap bond before any GF-17 firmware work.",
        "failed_primary": "Grounding strap bond measured 0.03 Ω, which is good. The strap was not the cause at this desert site and the fault persisted.",
    },
    "GF-17-alt": {
        "action": "Replace UV-cracked MC4 connector pair on the faulted string and re-test Riso string by string",
        "root_cause": "UV-degraded MC4 connector insulation on a single PV string (desert site)",
        "short": "UV-cracked MC4 connectors on one PV string (desert site variant)",
        "hours": 2.0, "cost": 60,
        "discovery": "isolated strings one at a time; string 7 read 48 kΩ. Found a UV-cracked MC4 connector pair on the underside of the rack",
        "check": "the string 7 MC4 pair was UV-cracked, reading 51 kΩ on that string",
        "tip": "GF-17 root cause is site-dependent: coastal sites mean grounding strap corrosion, desert sites mean UV-degraded MC4 connectors. Check strap bond first, then Riso string by string.",
    },
    "OT-05": {
        "action": "Recalibrate heatsink NTC in Diagnostics › NTC Cal against an IR thermometer and clean the intake filter",
        "root_cause": "Heatsink NTC sensor drift reading 6-8 °C high, triggering premature thermal derate",
        "short": "heatsink NTC sensor drift reading 6-8 °C high",
        "hours": 1.0, "cost": 0,
        "clue": "Fan RPM was 97% of rated and an IR gun read the heatsink 7 °C cooler than the controller.",
        "discovery": "compared the controller heatsink reading (83 °C) with an IR thermometer (76 °C): the NTC was reading 7 °C high while fans and airflow were normal",
        "check": "an IR thermometer read the heatsink 6 °C below the controller value",
        "tip": "On OT-05 with healthy fan RPM, compare the heatsink NTC with an IR gun before replacing the fan assembly.",
    },
    "F-082": {
        "action": "Re-terminate encoder cable shield with a 360° EMI clamp at the KDL16 drive and re-route the cable away from brake-coil wiring",
        "root_cause": "Unbonded encoder cable shield picking up EMI from brake-coil switching, producing false position deviations",
        "short": "unbonded encoder cable shield picking up brake-coil EMI",
        "hours": 1.5, "cost": 30,
        "clue": "Deviation events clustered around brake release.",
        "discovery": "logged deviation timestamps against brake events: 90% landed within 250 ms of brake-coil switching. The encoder shield clamp at the KDL16 drive was not bonded and the cable shared trunking with the brake feed",
        "check": "the encoder shield clamp at the drive was loose and the cable shared trunking with the brake feed",
        "tip": "Before replacing an MX20 encoder for F-082, check whether deviations coincide with brake switching and verify the shield's 360° bond.",
    },
}

ESCALATION = {
    "E-412": ("Replace compressor A1 current transducer and reload VFD parameters", 3.0, 380),
    "E-221": ("Replace LWT sensor harness and re-seat controller input connector J9", 1.5, 60),
    "GF-17": ("Replace DC input board INV9-DCB-02", 3.5, 1350),
    "OT-05": ("Replace heatsink thermal pad kit and reset derate counters", 3.0, 210),
    "F-082": ("Replace encoder coupling and reload drive parameters", 4.0, 320),
    "F-119": ("Replace brake release microswitch", 1.5, 95),
    "E-118": ("Replace condenser fan VFD", 3.0, 900),
    "COM-31": ("Replace RS-485 comms card", 1.5, 260),
}

CONTROL = {
    "E-118": ("Contactor K3 contacts were pitted; replaced and reset the fan VFD. All fans ran 10 minutes at 100% without fault.",
              "Pitted condenser fan contactor K3"),
    "COM-31": ("The 120 Ω termination was missing after a SCADA panel rework; restored it and reseated the card. Modbus polling stable.",
               "Missing RS-485 termination after panel rework"),
    "F-119": ("Air gap measured 0.52 mm; adjusted to 0.40 mm. 10 test cycles clean.",
              "Brake air gap drifted out of tolerance (0.52 mm)"),
}
CONTROL_ESCALATION = {
    "F-119": ("Release microswitch showed 9 ms contact bounce; replaced it. 10 test cycles clean.",
              "Worn brake release microswitch (9 ms contact bounce)"),
}

# (date, tech, code, unit or None for auto-assign, first-visit kind, first-visit held,
#  revisit (tech, date, kind) or None, flags)
# kinds: manual | manual_escalation | field | field_alt
SCHEDULE = [
    # ---- Week 1 (target FTF 11/18) -------------------------------------------------
    ("08-31", "D", "E-118", None, "manual", True, None, ""),
    ("08-31", "S", "F-119", None, "manual", True, None, ""),
    ("08-31", "A", "COM-31", None, "manual", True, None, ""),
    ("09-01", "A", "E-412", "CHL-0417", "manual", False, ("D", "09-03", "field"), ""),
    ("09-01", "S", "F-082", "ELV-0215", "manual", False, ("S", "09-02", "manual_escalation"), ""),
    ("09-02", "D", "GF-17", "INV-2231", "manual", False, ("S", "09-04", "field"), ""),
    ("09-02", "A", "F-119", None, "manual", True, None, ""),
    ("09-02", "S", "E-221", "CHL-0708", "manual", True, None, "genuine"),
    ("09-03", "D", "COM-31", None, "manual", True, None, ""),
    ("09-03", "A", "OT-05", "INV-3107", "manual", False, ("D", "09-05", "manual_escalation"), ""),
    ("09-03", "A", "GF-17", "INV-2240", "manual", False, ("S", "09-06", "field"), ""),
    ("09-04", "D", "E-118", None, "manual", True, None, ""),
    ("09-04", "A", "F-082", "ELV-0108", "manual", False, ("S", "09-06", "manual_escalation"), ""),
    ("09-05", "D", "E-412", "CHL-0522", "field", True, None, ""),
    ("09-05", "S", "F-119", None, "manual", True, None, ""),
    ("09-06", "D", "F-119", None, "manual", True, None, ""),
    ("09-06", "A", "E-221", "CHL-0631", "manual", False, ("D", "09-07", "manual_escalation"), ""),
    ("09-06", "S", "COM-31", None, "manual", True, None, ""),
    # ---- Week 2 (target FTF 13/18) -------------------------------------------------
    ("09-07", "D", "E-118", None, "manual", True, None, ""),
    ("09-07", "S", "COM-31", None, "manual", True, None, ""),
    ("09-08", "A", "F-082", "ELV-0108", "manual", False, ("S", "09-10", "field"), ""),
    ("09-08", "D", "GF-17", "INV-2252", "field", True, None, ""),
    ("09-08", "A", "E-412", "CHL-0631", "field", True, None, ""),
    ("09-09", "S", "E-221", "CHL-0522", "manual", False, ("A", "09-10", "field"), ""),
    ("09-09", "D", "F-119", None, "manual", True, None, ""),
    ("09-09", "A", "OT-05", "INV-3112", "manual", False, ("D", "09-11", "field"), ""),
    ("09-09", "D", "F-082", "ELV-0216", "manual", False, ("S", "09-11", "field"), ""),
    ("09-10", "D", "COM-31", None, "manual", True, None, ""),
    ("09-10", "S", "F-119", None, "manual", True, None, ""),
    ("09-10", "A", "GF-17", "INV-2267", "field", True, None, ""),
    ("09-11", "S", "E-412", "CHL-0708", "field", True, None, ""),
    ("09-11", "A", "E-118", None, "manual", True, None, ""),
    ("09-12", "S", "OT-05", "INV-3125", "field", True, None, ""),
    ("09-12", "A", "F-119", None, "manual", True, None, ""),
    ("09-12", "D", "E-221", "CHL-0815", "manual", False, ("A", "09-13", "field"), "no_copilot"),
    ("09-13", "S", "E-221", "CHL-0417", "field", True, None, ""),
    # ---- Week 3 (target FTF 15/18) -------------------------------------------------
    ("09-14", "A", "E-412", "CHL-0815", "field", True, None, ""),
    ("09-14", "S", "COM-31", None, "manual", True, None, ""),
    ("09-14", "D", "F-082", None, "field", True, None, ""),
    ("09-15", "A", "OT-05", "INV-3107", "field", True, None, "recurrence"),
    ("09-15", "D", "E-221", None, "field", True, None, ""),
    ("09-15", "S", "F-119", None, "manual", False, ("A", "09-16", "manual_escalation"), ""),
    ("09-16", "A", "GF-17", "INV-2275", "field", True, None, ""),
    ("09-16", "D", "E-118", None, "manual", True, None, ""),
    ("09-16", "S", "E-412", "CHL-0417", "field", False, ("S", "09-17", "manual"), "genuine"),
    ("09-17", "A", "F-082", None, "field", True, None, ""),
    ("09-17", "D", "OT-05", "INV-3133", "field", True, None, ""),
    ("09-17", "S", "E-221", None, "field", True, None, ""),
    ("09-18", "A", "F-119", None, "manual", True, None, ""),
    ("09-18", "D", "GF-17", "INV-2288", "field", True, None, ""),
    ("09-18", "S", "COM-31", None, "manual", True, None, ""),
    ("09-19", "A", "GF-17", "INV-3112", "field", False, ("S", "09-20", "field_alt"), ""),
    ("09-19", "S", "F-119", None, "manual", True, None, ""),
    ("09-20", "D", "E-118", None, "manual", True, None, ""),
    # ---- Week 4 (target FTF 17/18) -------------------------------------------------
    ("09-21", "A", "E-412", None, "field", True, None, ""),
    ("09-21", "S", "F-119", None, "manual", True, None, ""),
    ("09-21", "D", "GF-17", "INV-3125", "field_alt", True, None, ""),
    ("09-22", "A", "F-082", None, "field", True, None, ""),
    ("09-22", "D", "E-221", None, "field", True, None, ""),
    ("09-22", "S", "OT-05", "INV-2240", "field", True, None, ""),
    ("09-23", "A", "COM-31", None, "manual", True, None, ""),
    ("09-23", "D", "F-082", None, "field", True, None, ""),
    ("09-23", "S", "E-412", None, "field", True, None, ""),
    ("09-24", "A", "OT-05", None, "field", True, None, ""),
    ("09-24", "D", "E-221", None, "field", True, None, ""),
    ("09-24", "S", "F-082", None, "field", True, None, ""),
    ("09-25", "A", "E-118", None, "manual", True, None, ""),
    ("09-25", "D", "COM-31", None, "manual", True, None, ""),
    ("09-25", "S", "F-119", None, "manual", False, ("A", "09-26", "manual_escalation"), ""),
    ("09-26", "A", "COM-31", None, "manual", True, None, ""),
    ("09-26", "D", "F-119", None, "manual", True, None, ""),
    ("09-27", "S", "E-221", None, "field", True, None, ""),
]

# Units with a scripted demo scenario stay out of the auto-assign pool so their
# live telemetry is not contradicted by synthetic history.
DEMO_RESERVED = {"INV-3140"}


def _d(mmdd: str) -> date:
    month, day = mmdd.split("-")
    return date(YEAR, int(month), int(day))


def _ts(day: date, rng: random.Random) -> str:
    hour = rng.randint(13, 21)
    minute = rng.choice([0, 15, 30, 45])
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _week(day: date) -> int:
    return (day - START).days // 7 + 1


def _assign_units(rows: list[dict]) -> None:
    """Fill auto-assigned units: never repeat a unit+code within 10 days, spread usage."""
    pools = {
        model: sorted(uid for uid, u in CATALOG["units"].items() if u["model"] == model and uid not in DEMO_RESERVED)
        for model in CATALOG["models"]
    }
    usage: dict[str, int] = {}
    for row in rows:
        if row["unit"]:
            usage[row["unit"]] = usage.get(row["unit"], 0) + 1
    for row in rows:
        if row["unit"]:
            continue
        model = CODE_MODEL[row["code"]]

        def conflict(uid: str) -> bool:
            return any(
                other["unit"] == uid and other["code"] == row["code"] and abs((other["date"] - row["date"]).days) < 10
                for other in rows
                if other is not row
            )

        candidates = [u for u in pools[model] if not conflict(u)] or pools[model]
        choice = min(candidates, key=lambda u: (usage.get(u, 0), u))
        row["unit"] = choice
        usage[choice] = usage.get(choice, 0) + 1


def _site(unit_id: str) -> dict:
    return CATALOG["sites"][CATALOG["units"][unit_id]["site"]]


def _who(tech_id: str) -> str:
    return f"{tech_id} ({TECH_INFO[tech_id]['role']})"


def _avoided(code: str) -> str:
    manual = CATALOG["models"][CODE_MODEL[code]]["error_codes"][code]["manual"]
    if manual["parts"]:
        part = manual["parts"][0]
        return f"a {part['part']} (${part['cost_usd']:,}) and about {manual['est_labor_hours']:.0f} h of OEM procedure labor"
    return "a repeat of the OEM reset procedure and a likely board swap"


def build() -> dict:
    rng = random.Random(4127)
    rows = [
        {
            "date": _d(d), "tech": TECHS[t], "code": code, "unit": unit, "first_kind": kind,
            "first_held": held,
            "revisit": (TECHS[r[0]], _d(r[1]), r[2]) if r else None,
            "flags": set(filter(None, flags.split(","))),
        }
        for d, t, code, unit, kind, held, r, flags in SCHEDULE
    ]
    rows.sort(key=lambda r: r["date"])
    _assign_units(rows)

    discoveries: dict[str, dict] = {}  # knowledge key -> first field visit that held
    work_orders = []

    for idx, row in enumerate(rows, start=1):
        code, unit = row["code"], row["unit"]
        model_key = CODE_MODEL[code]
        model = CATALOG["models"][model_key]
        spec = model["error_codes"][code]
        manual = spec["manual"]
        site = _site(unit)
        wo_id = f"WO-{YEAR}-{idx:04d}"
        header = f"{code} ({spec['title']}) on {model['name']} unit {unit} at {site['name']}"

        plan = [(row["tech"], row["date"], row["first_kind"], row["first_held"])]
        if row["revisit"]:
            r_tech, r_date, r_kind = row["revisit"]
            plan.append((r_tech, r_date, r_kind, True))

        visits = []
        for n, (tech, day, kind, held) in enumerate(plan, start=1):
            ts = _ts(day, rng)
            prior = visits[-1] if visits else None
            callback = (
                f"Callback on {wo_id}: the {prior['timestamp'][:10]} visit by {prior['technician']} "
                f"({prior['action'][0].lower() + prior['action'][1:]}) did not resolve the fault. "
                if prior else ""
            )
            knowledge_key = code if kind != "field_alt" else f"{code}-alt"
            learned_from = None
            knowledge_source = "oem_manual"

            if kind in ("field", "field_alt"):
                fk = FIELD[knowledge_key]
                action, hours, cost = fk["action"], fk["hours"], fk["cost"]
                disc = discoveries.get(knowledge_key)
                if held and disc is None:
                    knowledge_source = "discovery"
                    notes = (
                        f"{callback}{_who(tech)} {fk['discovery']}. Performed: {action}. Root cause: {fk['root_cause']}. "
                        f"Outcome: fix HELD, 7-day follow-up clean. Field tip: {fk['tip']}"
                    )
                    root_cause = fk["root_cause"]
                elif disc is None:
                    raise ValueError(f"{wo_id}: field fix for {knowledge_key} before any discovery")
                else:
                    learned_from = {k: disc[k] for k in ("technician", "work_order", "date", "unit_id")}
                    knowledge_source = "self" if disc["technician"] == tech else "peer_memory"
                    recalled = (
                        f"Applied their own field finding from {disc['date']} ({disc['unit_id']})"
                        if knowledge_source == "self"
                        else f"Copilot recalled {disc['technician']}'s field finding from {disc['date']} on {disc['unit_id']}"
                    )
                    if held:
                        notes = (
                            f"{callback}{_who(tech)} responded to {header}. {recalled}: {fk['short']}. "
                            f"Checked that before the OEM step ({manual['primary_action']}) and confirmed {fk['check']}. "
                            f"Performed: {action}. Outcome: fix HELD, 7-day follow-up clean. Avoided {_avoided(code)}."
                        )
                        root_cause = fk["root_cause"]
                    else:
                        # Nothing was wrong where memory pointed, so nothing was replaced.
                        action, hours, cost = f"Inspected recalled field finding ({fk['short']}); no defect found", 0.75, 0
                        failure = fk.get("failed_primary") or (
                            f"Inspected per the recalled finding: no defect found there, torque verified. Fault persisted on restart, so "
                            f"the known field pattern was NOT the cause this time."
                        )
                        notes = f"{_who(tech)} responded to {header}. {recalled}: {fk['short']}. {failure} Outcome: did NOT hold; escalated."
                        root_cause = "unconfirmed"
                if held and knowledge_key not in discoveries:
                    discoveries[knowledge_key] = {
                        "technician": tech, "work_order": wo_id, "date": day.isoformat(), "unit_id": unit,
                    }
            elif kind == "manual_escalation":
                esc_action, hours, cost = ESCALATION[code]
                action = esc_action
                if code in CONTROL_ESCALATION:
                    detail, root_cause = CONTROL_ESCALATION[code]
                    notes = f"{callback}{_who(tech)} performed the next OEM step. {detail} Outcome: fix HELD."
                else:
                    notes = (
                        f"{callback}{_who(tech)} performed the next OEM step for {header}: {esc_action}. "
                        f"Fault cleared on departure; root cause NOT confirmed."
                    )
                    root_cause = f"unconfirmed (fault cleared after {esc_action.lower()})"
            else:  # manual
                action = manual["primary_action"]
                hours = manual["est_labor_hours"]
                cost = sum(p["cost_usd"] for p in manual["parts"])
                if code in TRICKY:
                    fk = FIELD[code]
                    if held:
                        notes = f"{callback}{_who(tech)} responded to {header}. Followed {manual['section']}. {fk['genuine']} Outcome: fix HELD."
                        root_cause = fk["genuine_cause"]
                    else:
                        recur = rng.randint(10, 60)
                        notes = (
                            f"{_who(tech)} responded to {header}. Followed OEM {manual['section']}: {action}. "
                            f"Fault cleared on departure but returned after {recur} h, so the fix did NOT hold. {fk['clue']}"
                        )
                        root_cause = "unconfirmed"
                else:
                    detail, cause = CONTROL[code]
                    if held:
                        notes = f"{_who(tech)} responded to {header}. Followed {manual['section']}. {detail} Outcome: fix HELD."
                        root_cause = cause
                    else:
                        recur = rng.randint(6, 30)
                        notes = (
                            f"{_who(tech)} responded to {header}. Followed {manual['section']}: {action}. "
                            f"Fault returned after {recur} h; did NOT hold."
                        )
                        root_cause = "unconfirmed"

            if n == 1 and "no_copilot" in row["flags"]:
                notes += " Did not consult the Copilot before this visit."
            if n == 1 and "recurrence" in row["flags"]:
                notes += " Recurrence: the previous fix on this unit cleared the fault without a confirmed root cause."

            visits.append({
                "visit": n,
                "technician": tech,
                "timestamp": ts,
                "action": action,
                "action_category": "field" if kind in ("field", "field_alt") else "manual",
                "procedure_ref": manual["section"] if kind in ("manual", "manual_escalation") else "field practice",
                "outcome_held": held,
                "root_cause": root_cause,
                "labor_hours": hours,
                "parts_cost_usd": cost,
                "knowledge_source": knowledge_source,
                "learned_from": learned_from,
                "copilot_consulted": "no_copilot" not in row["flags"] or n > 1,
                "notes": notes,
            })

        work_orders.append({
            "id": wo_id,
            "week": _week(row["date"]),
            "opened_at": visits[0]["timestamp"],
            "unit_id": unit,
            "site": site["name"],
            "model": model_key,
            "model_name": model["name"],
            "fleet": model["fleet"],
            "error_code": code,
            "error_title": spec["title"],
            "first_time_fix": visits[0]["outcome_held"],
            "resolved_root_cause": visits[-1]["root_cause"],
            "total_labor_hours": round(sum(v["labor_hours"] for v in visits), 2),
            "total_parts_cost_usd": sum(v["parts_cost_usd"] for v in visits),
            "visits": visits,
        })

    weeks = [
        {
            "week": w,
            "label": f"Week {w}",
            "start": (START + timedelta(days=7 * (w - 1))).isoformat(),
            "end": (START + timedelta(days=7 * w - 1)).isoformat(),
        }
        for w in range(1, 5)
    ]
    return {
        "generated_by": "data/generate_seed.py",
        "period": {"start": weeks[0]["start"], "end": weeks[-1]["end"]},
        "weeks": weeks,
        "discoveries": [{"knowledge_key": k, **v} for k, v in discoveries.items()],
        "work_orders": work_orders,
    }


if __name__ == "__main__":
    data = build()
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    by_week: dict[int, list[bool]] = {}
    for wo in data["work_orders"]:
        by_week.setdefault(wo["week"], []).append(wo["first_time_fix"])
    for week, fixes in sorted(by_week.items()):
        print(f"Week {week}: {sum(fixes)}/{len(fixes)} first-time fixes ({sum(fixes) / len(fixes):.0%})")
    visits = sum(len(wo["visits"]) for wo in data["work_orders"])
    print(f"Wrote {OUT.relative_to(HERE.parent)}: {len(data['work_orders'])} work orders, {visits} visits")
