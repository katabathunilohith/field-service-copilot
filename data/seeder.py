"""Preload the Hindsight memory bank with the seeded 4-week service history.

    python data/seeder.py --dry-run      # show what would be sent, no network
    python data/seeder.py                # create/update the bank profile and retain all records
    python data/seeder.py --no-wait      # queue batches and return without waiting for extraction
    python data/seeder.py --sync         # process each batch synchronously (slowest, simplest)

Re-running is safe: every record has a stable document_id (e.g. wo-2026-0004-v2),
so Hindsight replaces it instead of duplicating it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import RUNTIME_DIR, settings  # noqa: E402
from fleet.catalog import get_catalog  # noqa: E402
from memory.bank_schemas import RepairRecord, bank_schema, load_seed, records_from_seed, resolve_bank_id  # noqa: E402
from memory.field_notes import seed_notes  # noqa: E402
from memory.event_log import EventLog  # noqa: E402
from memory.fallback_store import LocalMemoryStore  # noqa: E402
from memory.hindsight_wrapper import HindsightError, HindsightMemory  # noqa: E402

# Must match the bank the backend reads (config.Settings.hindsight_bank_id).
BANK_ID = os.getenv("HINDSIGHT_BANK_ID", "field-service-copilot")


def plan(records: list[RepairRecord], base: str, per_fleet: bool) -> dict[str, list[RepairRecord]]:
    by_bank: dict[str, list[RepairRecord]] = defaultdict(list)
    for record in sorted(records, key=lambda r: r.occurred_at):  # chronological, like real life
        by_bank[resolve_bank_id(record.fleet, base=base, per_fleet=per_fleet)].append(record)
    return dict(by_bank)


async def seed(args: argparse.Namespace) -> int:
    notes = seed_notes(get_catalog())
    records = notes if args.notes_only else records_from_seed(load_seed()) + notes
    if args.limit:
        records = records[: args.limit]
    banks = plan(records, args.bank, settings.hindsight_per_fleet_banks)

    kinds = f"{sum(r.record_type == 'repair' for r in records)} repair records, {sum(r.record_type == 'note' for r in records)} field notes"
    print(f"Seed history: {kinds} → {', '.join(f'{b} ({len(r)})' for b, r in banks.items())}")
    if args.dry_run:
        sample = next(iter(banks.values()))[0].to_retain_item()
        print("\nDry run: no network calls. First retain item:\n")
        print(json.dumps(sample, indent=2, ensure_ascii=False))
        return 0

    if not settings.hindsight_enabled:
        print("HINDSIGHT_API_KEY is not set. Copy .env.example to .env and add your key, or use --dry-run.", file=sys.stderr)
        return 2

    # journal=False below means this store is never written; it only satisfies the constructor.
    memory = HindsightMemory(settings, event_log=EventLog(), fallback=LocalMemoryStore([], RUNTIME_DIR / "unused.jsonl"))
    failures = 0
    pending: list[tuple[str, str]] = []
    try:
        for bank_id, bank_records in banks.items():
            fleet = bank_id.removeprefix(f"{args.bank}-") if bank_id != args.bank else None
            try:
                setup = await memory.ensure_bank(bank_schema(bank_id, fleet))
                d = setup["directives"]
                print(f"\n[{bank_id}] bank ready · config patched · directives "
                      f"{d['created']} created, {d['updated']} updated, {d['unchanged']} unchanged")
            except (TimeoutError, HindsightError) as exc:
                print(f"\n[{bank_id}] could not set up bank: {exc}", file=sys.stderr)
                failures += 1
                continue

            for i in range(0, len(bank_records), args.batch_size):
                batch = bank_records[i : i + args.batch_size]
                result = await memory.retain_records(
                    bank_id, batch, journal=False, timeout_s=args.timeout,
                    retries=4, async_=not args.sync,
                )
                span = f"{batch[0].occurred_at[:10]} → {batch[-1].occurred_at[:10]}"
                if result.source == "hindsight":
                    op = f" op={result.operation_id}" if result.operation_id else ""
                    print(f"  batch {i // args.batch_size + 1:>2}: {len(batch):>2} records ({span}) {result.status}{op}")
                    if result.operation_id:
                        pending.append((bank_id, result.operation_id))
                else:
                    failures += 1
                    print(f"  batch {i // args.batch_size + 1:>2}: FAILED ({result.error})", file=sys.stderr)

        if pending and not args.no_wait:
            failures += await wait_for_operations(memory, pending, args.wait_timeout)
        for bank_id in banks:
            try:
                stats = await memory.bank_stats(bank_id)
                print(f"\n[{bank_id}] {stats.get('total_nodes', 0)} facts · {stats.get('total_documents', 0)} documents · "
                      f"{stats.get('total_observations', 0)} observations · {stats.get('pending_operations', 0)} pending ops")
            except (TimeoutError, HindsightError) as exc:
                print(f"\n[{bank_id}] stats unavailable: {exc}", file=sys.stderr)
    finally:
        await memory.aclose()

    if failures:
        print(f"\nDone with {failures} failure(s). Re-run to retry; document IDs make it idempotent.", file=sys.stderr)
        return 1
    if args.sync or not args.no_wait:
        print(f"\nAll {len(records)} records processed and recallable.")
    else:
        print(f"\nAll {len(records)} records queued (Hindsight extracts facts in the background; allow a few minutes).")
    return 0


async def wait_for_operations(memory: HindsightMemory, pending: list[tuple[str, str]], timeout_s: float) -> int:
    """Poll async retain operations until they finish. Returns the number that failed or timed out."""
    print(f"\nWaiting for Hindsight to extract facts from {len(pending)} batch(es)…")
    deadline = time.monotonic() + timeout_s
    remaining = dict.fromkeys(pending)
    failed = 0
    last_report = ""
    while remaining and time.monotonic() < deadline:
        for bank_id, op_id in list(remaining):
            try:
                status = (await memory.operation_status(bank_id, op_id)).get("status", "unknown")
            except (TimeoutError, HindsightError):
                continue  # transient; poll again next round
            if status == "completed":
                remaining.pop((bank_id, op_id))
            elif status in ("failed", "cancelled"):
                remaining.pop((bank_id, op_id))
                failed += 1
                print(f"  operation {op_id} {status}", file=sys.stderr)
        report = f"  {len(pending) - len(remaining)}/{len(pending)} batches done"
        if report != last_report:
            print(report)
            last_report = report
        if remaining:
            await asyncio.sleep(3)
    if remaining:
        print(f"  {len(remaining)} batch(es) still processing after {timeout_s:.0f}s; they will finish in the background.",
              file=sys.stderr)
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Hindsight bank with field service history")
    parser.add_argument("--bank", default=BANK_ID, help=f"base bank id (default: {BANK_ID})")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per batch request")
    parser.add_argument("--sync", action="store_true", help="wait for extraction on each batch")
    parser.add_argument("--limit", type=int, default=0, help="only seed the first N records")
    parser.add_argument("--notes-only", action="store_true",
                        help="only seed the technicians' field notes (site rules, hazards, equipment quirks)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and a sample payload without calling Hindsight")
    parser.add_argument("--no-wait", action="store_true", help="return as soon as batches are queued")
    parser.add_argument("--wait-timeout", type=float, default=900.0, help="seconds to wait for background extraction")
    sys.exit(asyncio.run(seed(parser.parse_args())))


if __name__ == "__main__":
    main()
