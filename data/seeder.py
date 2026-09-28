"""Preload the Hindsight memory bank with the seeded 4-week service history.

    python data/seeder.py --dry-run      # show what would be sent, no network
    python data/seeder.py                # create/update the bank profile and retain all records
    python data/seeder.py --sync         # wait for fact extraction on each batch (slower)

Re-running is safe: every record has a stable document_id (e.g. wo-2026-0004-v2),
so Hindsight replaces it instead of duplicating it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import RUNTIME_DIR, settings  # noqa: E402
from memory.bank_schemas import RepairRecord, bank_schema, load_seed, records_from_seed, resolve_bank_id  # noqa: E402
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
    records = records_from_seed(load_seed())
    if args.limit:
        records = records[: args.limit]
    banks = plan(records, args.bank, settings.hindsight_per_fleet_banks)

    print(f"Seed history: {len(records)} repair records → {', '.join(f'{b} ({len(r)})' for b, r in banks.items())}")
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
    try:
        for bank_id, bank_records in banks.items():
            fleet = bank_id.removeprefix(f"{args.bank}-") if bank_id != args.bank else None
            try:
                await memory.ensure_bank(bank_schema(bank_id, fleet))
                print(f"\n[{bank_id}] bank profile ready")
            except (TimeoutError, HindsightError) as exc:
                print(f"\n[{bank_id}] could not create/update bank profile: {exc}", file=sys.stderr)
                failures += 1
                continue

            for i in range(0, len(bank_records), args.batch_size):
                batch = bank_records[i : i + args.batch_size]
                result = await memory.retain_records(
                    bank_id, batch, journal=False, timeout_s=args.timeout, retry_in_background=False,
                    retries=4, async_=not args.sync,
                )
                span = f"{batch[0].occurred_at[:10]} → {batch[-1].occurred_at[:10]}"
                if result.source == "hindsight":
                    op = f" op={result.operation_id}" if result.operation_id else ""
                    print(f"  batch {i // args.batch_size + 1:>2}: {len(batch):>2} records ({span}) {result.status}{op}")
                else:
                    failures += 1
                    print(f"  batch {i // args.batch_size + 1:>2}: FAILED ({result.error})", file=sys.stderr)
    finally:
        await memory.aclose()

    if failures:
        print(f"\nDone with {failures} failure(s). Re-run to retry; document IDs make it idempotent.", file=sys.stderr)
        return 1
    mode = "processed" if args.sync else "queued (Hindsight extracts facts in the background; allow a minute before recall)"
    print(f"\nAll {len(records)} records {mode}.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Hindsight bank with field service history")
    parser.add_argument("--bank", default=BANK_ID, help=f"base bank id (default: {BANK_ID})")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per batch request")
    parser.add_argument("--sync", action="store_true", help="wait for extraction on each batch")
    parser.add_argument("--limit", type=int, default=0, help="only seed the first N records")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and a sample payload without calling Hindsight")
    sys.exit(asyncio.run(seed(parser.parse_args())))


if __name__ == "__main__":
    main()
