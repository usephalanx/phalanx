#!/usr/bin/env python3
"""P0-2 — operator tool: parse and validate ledger.jsonl.

Reports:
  - total lines
  - valid JSON lines
  - corrupt lines (printed with byte offsets so you can `dd` them out)
  - unique ledger_ids
  - rows per verdict
  - last 5 entries (id + verdict + exported_at)
  - schema version distribution

Exits 0 if every line parses, 1 if any corrupt lines found.

The point is to make corruption *visible*. The append path is designed
so corrupt tails never poison earlier evidence, but the operator still
needs to know when something went wrong.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def verify(path: Path) -> int:
    if not path.exists():
        print(f"ERROR: {path} does not exist", file=sys.stderr)
        return 1

    total = 0
    valid = 0
    corrupt: list[tuple[int, int, str]] = []  # (line_no, byte_offset, snippet)
    ledger_ids: set[str] = set()
    verdicts: Counter = Counter()
    schemas: Counter = Counter()
    last_5: list[dict] = []

    byte_offset = 0
    with path.open("rb") as f:
        for line_no, raw in enumerate(f, start=1):
            total += 1
            line_offset = byte_offset
            byte_offset += len(raw)
            try:
                entry = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                snippet = raw[:60].decode("utf-8", errors="replace").rstrip()
                corrupt.append((line_no, line_offset, f"{type(e).__name__}: {snippet!r}"))
                continue

            valid += 1
            row = entry.get("row") or {}
            lid = row.get("id")
            if lid:
                ledger_ids.add(lid)
            v = row.get("phalanx_verdict") or "<null>"
            verdicts[v] += 1
            schemas[entry.get("_schema_version", "<missing>")] += 1
            last_5.append({
                "id": lid,
                "verdict": v,
                "exported_at": entry.get("_exported_at"),
                "exported_by": entry.get("_exported_by"),
            })

    print(f"path                : {path}")
    print(f"total lines         : {total}")
    print(f"valid JSON          : {valid}")
    print(f"corrupt lines       : {len(corrupt)}")
    print(f"unique ledger_ids   : {len(ledger_ids)}")
    print(f"schema versions     : {dict(schemas)}")
    print("verdict counts      :")
    for v, c in verdicts.most_common():
        print(f"  {v:<24s} {c}")

    if last_5:
        print("\nlast 5 entries:")
        for e in last_5[-5:]:
            print(f"  {e['exported_at']}  {e['exported_by']:<22s} "
                  f"{(e['id'] or '<no-id>')[:8]}  {e['verdict']}")

    if corrupt:
        print("\ncorrupt lines (one per line — line_no, byte_offset, error):", file=sys.stderr)
        for line_no, off, msg in corrupt:
            print(f"  line={line_no}  byte_offset={off}  {msg}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify ledger.jsonl integrity")
    ap.add_argument(
        "path",
        nargs="?",
        default="ledger.jsonl",
        help="path to ledger.jsonl (default: ./ledger.jsonl)",
    )
    args = ap.parse_args()
    return verify(Path(args.path))


if __name__ == "__main__":
    raise SystemExit(main())
