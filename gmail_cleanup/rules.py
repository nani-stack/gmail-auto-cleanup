"""Turn probabilities into one of three actions.

The model judges; this module decides. Anything involving dates, counting or a
sender list stays here in plain code, where it is exact and auditable.
"""

from __future__ import annotations

from .config import Config


def decide(email: dict, cfg: Config, cleanup_below: float) -> tuple[str, str]:
    """Return (action, reason) for one judged email.

    KEEP leaves it alone, CLEANUP archives it, REVIEW labels it and leaves it
    in the inbox for a human.
    """
    if "STARRED" in email.get("labels", []):
        return "KEEP", "starred"
    if "error" in email:
        return "KEEP", "judgement failed"

    sender = email["from"].lower()
    for pattern in cfg.always_clean:
        if pattern in sender:
            return "CLEANUP", f"sender {pattern}"

    # Cleanup rules fire even on mail that looks important, unless a keep
    # question they name vetoes them.
    for q in cfg.cleanup_questions:
        if email.get(q.name, 0) < q.at:
            continue
        if q.older_than_days and email.get("age_days", 0) <= q.older_than_days:
            continue
        if q.unless and email.get(q.unless, 0) >= cfg.keep_at:
            continue
        return "CLEANUP", f"{q.name} {email[q.name]:.2f}"

    top = max(cfg.keep_questions, key=lambda q: email.get(q.name, 0))
    p = email.get(top.name, 0)
    if p >= cfg.keep_at:
        return "KEEP", f"{top.name} {p:.2f}"
    if p < cleanup_below:
        return "CLEANUP", f"max {top.name} {p:.2f}"
    return "REVIEW", f"{top.name} {p:.2f}"
