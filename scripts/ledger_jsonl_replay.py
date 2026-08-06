#!/usr/bin/env python3
"""P0-2 — operator tool: replay every shadow_ledger row from the DB into
ledger.jsonl with `_exported_by='replay'`.

Use cases:
  - jsonl file was lost or corrupted; recover from DB
  - jsonl file's last few lines were truncated; backfill to be safe
  - new field added to to_dict; rewrite history with the new schema

This APPENDS to whatever's already in the file. To start fresh, move
the existing file aside first.

The DB is the source of truth. The jsonl is the durable evidence stream.
Replay reconciles them.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from phalanx.db.models import ShadowLedger
from phalanx.db.session import AsyncSessionLocal
from phalanx.shadow.ledger import to_dict
from phalanx.shadow.ledger_export import append_ledger_row_async


async def replay(limit: int | None) -> int:
    appended = 0
    async with AsyncSessionLocal() as session:
        stmt = select(ShadowLedger).order_by(ShadowLedger.created_at.asc())
        if limit:
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        for row in rows:
            ok = await append_ledger_row_async(to_dict(row), exported_by="replay")
            if ok:
                appended += 1
            else:
                print(
                    f"WARN: failed to append ledger_id={row.id}; continuing",
                    file=sys.stderr,
                )
    return appended


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Replay shadow_ledger rows into ledger.jsonl")
    ap.add_argument("--limit", type=int, default=None, help="max rows (default: all)")
    args = ap.parse_args()

    appended = asyncio.run(replay(args.limit))
    print(f"OK appended {appended} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
