"""Command line entry point.

    python -m gmail_cleanup                 # dry run over the bulk tabs
    python -m gmail_cleanup --apply         # actually label and archive
    python -m gmail_cleanup --primary       # the Primary tab, stricter bar
    python -m gmail_cleanup --review        # re-judge mail labeled earlier
    python -m gmail_cleanup --trash-cleanup # trash cleanup mail past its grace period
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime
from pathlib import Path

from . import gmail, judge, rules
from . import config as config_module

CHUNK = 200  # emails fetched, judged and applied per pass


def _review_query(cfg) -> str:
    return (f'(label:"{cfg.labels["REVIEW"]}" OR label:"{cfg.labels["KEEP"]}") -is:starred')


def _exclusions(cfg) -> str:
    return " ".join(f'-label:"{name}"' for name in cfg.labels.values())


def trash_cleanup(box: gmail.Mailbox, cfg, directory: Path, apply_changes: bool) -> None:
    """Trash mail that has carried the cleanup label for the configured grace period.

    A ledger records when each message was first seen with the label, since
    Gmail does not report when a label was added.
    """
    ledger_path = directory / "cleanup_ledger.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    now = time.time()
    current = set(box.message_ids(f'label:"{cfg.labels["CLEANUP"]}" -in:trash', 100_000))
    for mid in current:
        ledger.setdefault(mid, now)

    due = [mid for mid in current if now - ledger[mid] >= cfg.trash_after_days * 86400]
    print(f'{cfg.labels["CLEANUP"]}: {len(current)} labeled, {len(due)} past '
          f"{cfg.trash_after_days:g} days, {len(current) - len(due)} still waiting")

    if apply_changes:
        for n, mid in enumerate(due, 1):
            box.trash(mid)
            if n % 100 == 0:
                print(f"  trashed {n}/{len(due)}", flush=True)
        for mid in due:
            ledger.pop(mid, None)
        print(f"Trashed {len(due)} messages; recoverable from Trash for 30 days.")
        current -= set(due)

    ledger_path.write_text(json.dumps({k: v for k, v in ledger.items() if k in current}))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="gmail_cleanup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml", help="path to the config file")
    ap.add_argument("--limit", type=int, default=50, help="max emails to process (default 50)")
    ap.add_argument("--apply", action="store_true", help="make the changes in Gmail")
    ap.add_argument("--primary", action="store_true",
                    help="triage the Primary tab, where only clear junk is archived")
    ap.add_argument("--review", action="store_true", help="re-judge mail labeled by earlier runs")
    ap.add_argument("--trash-cleanup", action="store_true",
                    help="trash cleanup mail past its grace period, then exit")
    args = ap.parse_args(argv)

    config_path = Path(args.config).expanduser()
    cfg = config_module.load(config_path)
    directory = config_path.resolve().parent

    # A .env beside the config is a convenient place to keep TYPESAFE_API_KEY.
    try:
        from dotenv import load_dotenv
        load_dotenv(directory / ".env")
    except ImportError:
        pass

    box = gmail.Mailbox(gmail.credentials(directory), cfg.requests_per_second)

    if args.trash_cleanup:
        trash_cleanup(box, cfg, directory, args.apply)
        if not args.apply:
            print("\nDry run: nothing trashed. Re-run with --apply.")
        return

    if args.review:
        query = _review_query(cfg)
    elif args.primary:
        if "primary" not in cfg.queries:
            raise SystemExit("config has no gmail.queries.primary")
        query = f"{cfg.queries['primary']} {_exclusions(cfg)}"
    else:
        query = f"{cfg.queries['bulk']} {_exclusions(cfg)}"

    cleanup_below = cfg.primary_cleanup_below if args.primary else cfg.cleanup_below

    print(f"Listing up to {args.limit} emails matching: {query}")
    ids = box.message_ids(query, args.limit)
    if not ids:
        print("Nothing to triage.")
        return

    model = judge.build(cfg)
    labels = box.label_ids(cfg.labels) if args.apply else None

    log_path = directory / f"triage_{datetime.now():%Y%m%d_%H%M%S}.log"
    fields = ["action", "reason", "from", "subject",
              *[q.name for q in cfg.all_questions], "model", "error", "id"]
    totals = {"CLEANUP": 0, "REVIEW": 0, "KEEP": 0}
    errors = 0

    with log_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()

        for start in range(0, len(ids), CHUNK):
            emails = box.fetch(ids[start:start + CHUNK])
            model.judge(emails)
            for e in emails:
                e["action"], e["reason"] = rules.decide(e, cfg, cleanup_below)
                totals[e["action"]] += 1
                errors += "error" in e
            if args.apply:
                box.apply(emails, labels, clear_prior=args.review)

            for e in emails:  # cleanup is bulk, so only show what survived
                if e["action"] != "CLEANUP":
                    print(f'{e["action"]:<7} {e["reason"]:<26} {e["from"][:28]:<28} '
                          f'{e["subject"][:50]}')
            print(f"-- {start + len(emails)}/{len(ids)} done | cleanup {totals['CLEANUP']}"
                  f" review {totals['REVIEW']} keep {totals['KEEP']}", flush=True)
            writer.writerows(emails)
            f.flush()

    print(f"\nCLEANUP {totals['CLEANUP']}  REVIEW {totals['REVIEW']}  KEEP {totals['KEEP']}")
    print(f"backend: {cfg.backend}   judgement errors: {errors}")
    print(f"Details saved to {log_path.name}")
    if not args.apply:
        print("\nDry run: nothing changed. Re-run with --apply to label and archive.")


if __name__ == "__main__":
    main()
