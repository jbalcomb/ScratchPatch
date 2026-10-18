"""CLI: ``python -m taskdeck.review [--json] [--out FILE] [--strict]``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from taskdeck.core.plugin import PluginRegistry

from .analyzer import review, summary_counts, to_markdown


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m taskdeck.review", description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    ap.add_argument("--out", type=Path, help="write the report here instead of stdout")
    ap.add_argument("--threshold", type=float, default=0.85, help="similarity (0-1) that counts as duplicate code")
    ap.add_argument("--min-tokens", type=int, default=40, help="ignore functions smaller than this")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on duplicate code, hygiene issues or load errors (for CI gating)")
    args = ap.parse_args(argv)

    report = review(PluginRegistry().discover(), args.threshold, args.min_tokens)
    text = json.dumps(report.to_dict(), indent=2) if args.json else to_markdown(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)

    counts = summary_counts(report)
    if args.strict and (counts["duplicates"] or counts["issues"] or counts["load_errors"]):
        print(f"plugin review failed: {dict(counts)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
