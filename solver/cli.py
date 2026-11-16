"""Run the Candace solver on a case file.

    python -m solver.cli solver/cases/2026-11-12/case.json [--include-unconfirmed] [--json out.json]

By default only confirmed rules are applied. --include-unconfirmed is a dry run
that also applies rules still waiting for the admin's confirmation.
"""
import argparse
import json
import sys
import time

from .model import load_case
from .solve import Options, solve


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("case")
    ap.add_argument("--include-unconfirmed", action="store_true")
    ap.add_argument("--time-limit", type=float, default=20.0)
    ap.add_argument("--json", help="write the full result to this file")
    a = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time()
    problem = load_case(a.case, include_unconfirmed=a.include_unconfirmed)
    result = solve(problem, Options(time_limit_s=a.time_limit))
    elapsed = time.time() - t0

    print(f"{result['status']} in {elapsed:.1f}s · rules applied: {len(problem.rules)}"
          f"{' (incl. unconfirmed — dry run)' if a.include_unconfirmed else ''}")
    if result.get("summary"):
        print("summary:", ", ".join(f"{k} {v}" for k, v in result["summary"].items()))
    for c in sorted(result.get("cells", []), key=lambda c: (c["sheet"], c["section"], c["period"] or 0)):
        struck = f"  [ABS: {', '.join(c['struck'])}]" if c["struck"] else ""
        why = f"  ({c['reason']})" if c.get("reason") else ""
        print(f"{c['section']:>5} P{c['period']}: {' + '.join(c['codes']) or '—':<12} {c['after'] or ''}{struck}{why}")
    over = {t: n for t, n in result.get("teacher_loads", {}).items() if n >= 5}
    if over:
        print("teachers at 5+ lectures:", over)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1, ensure_ascii=False)
    return 0 if result["status"] in ("OPTIMAL", "FEASIBLE") else 1


if __name__ == "__main__":
    sys.exit(main())
