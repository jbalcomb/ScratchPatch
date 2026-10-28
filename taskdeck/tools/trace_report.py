"""Summarize TaskDeck task traces: ``python tools/trace_report.py TRACE.jsonl [...] [--minute]``.

The traces are in the phone's diagnostics folder and in My Drive > TaskDeck diagnostics (next to a
``.summary.txt`` the app writes with the same report). See docs/DIAGNOSTICS.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taskdeck.core.trace_report import load, report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("trace", type=Path, nargs="+")
    ap.add_argument("--minute", action="store_true", help="print every heartbeat, not only stalls")
    args = ap.parse_args(argv)
    for i, path in enumerate(args.trace):
        if i:
            print("\n" + "=" * 80 + "\n")
        print(f"{path.name}\n{report(load(path), args.minute)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
