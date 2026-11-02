"""Turn triage logs into a Laya finetuning set.

Every run logged the hosted model's probability for each question, so the logs
are already a labelled corpus: distil those judgements into a local model.

    python tools/build_dataset.py --logs ~/Jev --config config.yaml --out data/

Writes train.jsonl and val.jsonl in the `{state, questions, gold}` schema that
`laya-train` reads, plus questions.json to use at inference time so the model
is asked exactly what it was trained on.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

def load_questions(config_path: str) -> dict[str, dict]:
    """The questions to train on, taken from the same config the triage runs use.

    Training on one wording and serving another teaches the model a question it
    will never be asked, so both sides read this one file.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gmail_cleanup import config as config_module
    from gmail_cleanup.judge import laya_questions

    cfg = config_module.load(Path(config_path).expanduser())
    return laya_questions(cfg.all_questions)


def rows_from_logs(log_dir: Path, QUESTIONS: dict) -> dict[str, dict]:
    """Latest judgement per message id, across every log in the directory."""
    best: dict[str, dict] = {}
    files = sorted(glob.glob(str(log_dir / "triage_*.log")), key=os.path.getmtime)
    for path in files:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if not row.get("id") or row.get("error"):
                    continue
                if not row.get("from") or not row.get("subject"):
                    continue
                labelled = sum(1 for q in QUESTIONS if _prob(row.get(q)) is not None)
                if not labelled:
                    continue
                prior = best.get(row["id"])
                # Later runs asked more questions; prefer the richer row.
                if prior is None or labelled >= prior["_labelled"]:
                    row["_labelled"] = labelled
                    best[row["id"]] = row
    print(f"read {len(files)} log files -> {len(best)} unique emails")
    return best


def _prob(value) -> float | None:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return p if 0.0 <= p <= 1.0 else None


def to_example(row: dict, QUESTIONS: dict) -> dict:
    gold = {}
    for name in QUESTIONS:
        p = _prob(row.get(name))
        if p is not None:
            gold[name] = {"probabilities": {"true": p, "false": 1.0 - p}}
    return {
        "state": {"sender": row["from"], "subject": row["subject"]},
        "questions": {k: v for k, v in QUESTIONS.items() if k in gold},
        "gold": gold,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", required=True, help="directory holding triage_*.log files")
    ap.add_argument("--config", default="config.yaml",
                    help="config whose questions the model is trained on")
    ap.add_argument("--out", default="data", help="directory to write the dataset into")
    ap.add_argument("--val-fraction", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    log_dir = Path(args.logs).expanduser()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    QUESTIONS = load_questions(args.config)
    print(f"training on {len(QUESTIONS)} questions from {args.config}")
    rows = rows_from_logs(log_dir, QUESTIONS)
    examples = [to_example(r, QUESTIONS) for r in rows.values()]
    random.Random(args.seed).shuffle(examples)

    # Split by a hash of the sender, so the same sender never appears in both
    # halves: otherwise the model scores well by memorising senders.
    def in_val(ex: dict) -> bool:
        sender = ex["state"]["sender"].lower()
        digest = hashlib.sha256(sender.encode()).hexdigest()
        return int(digest[:8], 16) / 0xFFFFFFFF < args.val_fraction

    val = [e for e in examples if in_val(e)]
    train = [e for e in examples if not in_val(e)]

    for name, split in (("train", train), ("val", val)):
        path = out_dir / f"{name}.jsonl"
        with path.open("w") as f:
            for ex in split:
                f.write(json.dumps(ex) + "\n")
        labels = Counter(q for ex in split for q in ex["gold"])
        positives = Counter(q for ex in split for q, g in ex["gold"].items()
                            if g["probabilities"]["true"] >= 0.5)
        print(f"\n{path}: {len(split)} emails, {sum(labels.values())} labelled questions")
        for q in QUESTIONS:
            n, pos = labels[q], positives[q]
            if n:
                print(f"  {q:<22} {n:>6} labels, {pos:>5} positive ({pos / n:.1%})")

    (out_dir / "questions.json").write_text(json.dumps(QUESTIONS, indent=1))
    print(f"\nwrote {out_dir/'questions.json'} - ask these same questions at inference time")


if __name__ == "__main__":
    main()
