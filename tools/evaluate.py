"""Score a Laya checkpoint against the hosted model's logged judgements.

    python tools/evaluate.py --model convaiinnovations/laya --data data/val.jsonl
    python tools/evaluate.py --model out/finetuned      --data data/val.jsonl

Reports, per question, how well the local model reproduces the teacher, and
how often the whole pipeline would take the same action. Accuracy alone is
useless here - a question with 1% positives scores 99% by always saying no -
so this also reports precision, recall and Brier score.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

KEEP_SIGNALS = ("ai_product", "oregon_state_related", "important", "personal")
CLEANUP_SIGNALS = {"college_recruiting": 0.72, "valley_catholic": 0.7, "verification_code": 0.7}
KEEP_AT, CLEANUP_BELOW = 0.5, 0.15


def action(probs: dict[str, float]) -> str:
    """The pipeline's decision, given one email's probabilities.

    Age is unavailable here, so the verification-code rule is treated as if the
    mail were old; it is the model's probability being compared, not the clock.
    """
    for name, at in CLEANUP_SIGNALS.items():
        if probs.get(name, 0) >= at:
            if name == "college_recruiting" and probs.get("oregon_state_related", 0) >= KEEP_AT:
                continue
            return "CLEANUP"
    present = [n for n in KEEP_SIGNALS if n in probs]
    if not present:
        return "REVIEW"
    top = max(present, key=lambda n: probs[n])
    if probs[top] >= KEEP_AT:
        return "KEEP"
    return "CLEANUP" if probs[top] < CLEANUP_BELOW else "REVIEW"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="convaiinnovations/laya")
    ap.add_argument("--subfolder", default=None)
    ap.add_argument("--data", default="data/val.jsonl")
    ap.add_argument("--limit", type=int, default=0, help="score only the first N emails")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="probability counted as a yes when scoring each question")
    args = ap.parse_args()

    import laya

    rows = [json.loads(line) for line in Path(args.data).read_text().splitlines() if line.strip()]
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} emails from {args.data}")

    agent = laya.load(args.model, subfolder=args.subfolder) if args.subfolder \
        else laya.load(args.model)

    states = [r["state"] for r in rows]
    questions = rows[0]["questions"]
    for r in rows:  # ask every question of every email, so answers line up
        questions.update(r["questions"])

    t0 = time.time()
    results = agent.predict_batch(states, questions, batch_size=args.batch_size)
    elapsed = time.time() - t0
    print(f"scored in {elapsed:.0f}s ({elapsed / max(len(rows), 1) * 1000:.0f} ms/email)\n")

    stats = {q: {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "sq": 0.0, "n": 0} for q in questions}
    same = total = 0
    confusion: dict[tuple[str, str], int] = {}

    for row, res in zip(rows, results):
        teacher = {q: g["probabilities"]["true"] for q, g in row["gold"].items()}
        student = {q: res["answers"][q]["noul"] for q in questions if q in res["answers"]}

        for q, p_teacher in teacher.items():
            p = student.get(q)
            if p is None:
                continue
            s = stats[q]
            s["n"] += 1
            s["sq"] += (p - p_teacher) ** 2
            yes_t, yes_s = p_teacher >= 0.5, p >= args.threshold
            s["tp"] += yes_t and yes_s
            s["fp"] += (not yes_t) and yes_s
            s["fn"] += yes_t and not yes_s
            s["tn"] += (not yes_t) and (not yes_s)

        # Compare actions only where the teacher labelled every question it needs.
        if len(teacher) == len(questions):
            a_t, a_s = action(teacher), action(student)
            same += a_t == a_s
            total += 1
            confusion[(a_t, a_s)] = confusion.get((a_t, a_s), 0) + 1

    print(f'{"question":<22} {"n":>6} {"pos":>5} {"prec":>6} {"recall":>7} {"brier":>7}')
    for q, s in stats.items():
        if not s["n"]:
            continue
        pos = s["tp"] + s["fn"]
        prec = s["tp"] / (s["tp"] + s["fp"]) if s["tp"] + s["fp"] else float("nan")
        rec = s["tp"] / pos if pos else float("nan")
        print(f'{q:<22} {s["n"]:>6} {pos:>5} {prec:>6.2f} {rec:>7.2f} {s["sq"] / s["n"]:>7.3f}')

    if total:
        print(f"\nsame action on {same}/{total} ({same / total:.1%})")
        print("teacher -> student, where they differ:")
        for (a_t, a_s), n in sorted(confusion.items(), key=lambda kv: -kv[1]):
            if a_t != a_s:
                print(f"  {a_t:<8} -> {a_s:<8} {n}")
    print("\nBrier is mean squared error against the teacher's probability; lower is better.")


if __name__ == "__main__":
    main()
