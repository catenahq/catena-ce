#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Advance-warning guard for time-boxed Trivy suppressions.

.trivyignore.yaml entries carry an `expiredAt: YYYY-MM-DD`. When the date
passes, Trivy itself re-fires the CVE -- but with no warning, a suppression
can lapse the day a release is cut and redden an unrelated PR. This check
adds the missing advance signal:

  - Any entry ALREADY past expiredAt -> exit 1 (a dead suppression: rebuild
    the image + drop the entry, or the next Trivy run fails anyway).
  - Any entry within --days of expiry -> a ::warning:: annotation, exit 0
    (never blocks a merge; nags on the weekly scheduled run + in the PR).

Run: uv run .github/scripts/check_trivyignore_expiry.py [--days N] [PATH]
Default PATH is .trivyignore.yaml in the current directory.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import yaml


def _parse(d: str) -> date:
    return datetime.strptime(str(d).strip(), "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=".trivyignore.yaml")
    ap.add_argument("--days", type=int, default=30,
                    help="advance-warning window (default 30)")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"no {path} -- nothing to check.")
        return 0

    doc = yaml.safe_load(path.read_text()) or {}
    vulns = doc.get("vulnerabilities") or []
    today = date.today()

    expired: list[tuple[str, date]] = []
    soon: list[tuple[str, date, int]] = []
    for v in vulns:
        exp = v.get("expiredAt")
        vid = str(v.get("id", "?"))
        if not exp:
            # No expiry = a permanent suppression, which defeats the time-box.
            print(f"::warning file={path}::{vid} has no expiredAt "
                  f"(permanent suppression; time-box it).")
            continue
        try:
            when = _parse(exp)
        except ValueError:
            print(f"::error file={path}::{vid} has an unparseable "
                  f"expiredAt {exp!r} (want YYYY-MM-DD).", file=sys.stderr)
            return 1
        delta = (when - today).days
        if delta < 0:
            expired.append((vid, when))
        elif delta <= args.days:
            soon.append((vid, when, delta))

    for vid, when, delta in sorted(soon, key=lambda t: t[2]):
        print(f"::warning file={path}::{vid} suppression expires {when} "
              f"(in {delta} day(s)) -- rebuild the image to clear the CVE for "
              f"real, do NOT just renew the date.")

    if expired:
        print(f"::error file={path}::{len(expired)} EXPIRED Trivy "
              f"suppression(s); Trivy will re-fire these CVEs. Rebuild the "
              f"affected image(s) + drop the dead entries:", file=sys.stderr)
        for vid, when in sorted(expired, key=lambda t: t[1]):
            print(f"  - {vid} (expired {when})", file=sys.stderr)
        return 1

    kept = len(vulns) - len(expired)
    print(f"trivyignore expiry OK: {kept} live suppression(s), "
          f"{len(soon)} within {args.days}d (warned), 0 expired.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
