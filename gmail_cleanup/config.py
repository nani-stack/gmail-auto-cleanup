"""Load and validate config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Question:
    """One yes/no judgement, asked of whichever model is configured."""

    name: str
    instructions: str
    criteria: dict[str, str] | None = None
    # Cleanup questions only:
    at: float = 0.7              # probability at or above which the rule fires
    older_than_days: float = 0   # only archive mail at least this old
    unless: str | None = None    # a keep question that vetoes this rule


@dataclass
class Config:
    backend: str
    backend_options: dict[str, Any]
    queries: dict[str, str]
    labels: dict[str, str]
    requests_per_second: float
    keep_at: float
    cleanup_below: float
    primary_cleanup_below: float
    trash_after_days: float
    always_clean: list[str]
    keep_questions: list[Question] = field(default_factory=list)
    cleanup_questions: list[Question] = field(default_factory=list)

    @property
    def all_questions(self) -> list[Question]:
        return self.keep_questions + self.cleanup_questions


def _criteria(raw: dict[Any, str] | None) -> dict[str, str] | None:
    """YAML reads bare `true:` and `false:` keys as booleans, so put them back."""
    if not raw:
        return None
    criteria = {str(k).lower(): v for k, v in raw.items()}
    unknown = set(criteria) - {"true", "false"}
    if unknown:
        raise ValueError(f"criteria keys must be 'true' or 'false', got {sorted(unknown)}")
    return criteria


def _questions(raw: dict[str, Any] | None, cleanup: bool) -> list[Question]:
    out = []
    for name, q in (raw or {}).items():
        if not q.get("instructions"):
            raise ValueError(f"question '{name}' needs instructions")
        out.append(Question(
            name=name,
            instructions=q["instructions"],
            criteria=_criteria(q.get("criteria")),
            at=float(q.get("at", 0.7)) if cleanup else 0.0,
            older_than_days=float(q.get("older_than_days", 0)) if cleanup else 0.0,
            unless=q.get("unless") if cleanup else None,
        ))
    return out


def load(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise SystemExit(
            f"No config at {path}. Copy config.example.yaml to config.yaml and edit it."
        )
    raw = yaml.safe_load(path.read_text()) or {}

    backend = raw.get("backend", "jev")
    if backend not in ("jev", "laya"):
        raise ValueError(f"backend must be 'jev' or 'laya', not {backend!r}")

    gmail = raw.get("gmail", {})
    thresholds = raw.get("thresholds", {})
    questions = raw.get("questions", {})

    cfg = Config(
        backend=backend,
        backend_options=raw.get(backend) or {},
        queries=gmail.get("queries", {}),
        labels={
            "KEEP": gmail.get("labels", {}).get("keep", "Triage/Keep"),
            "REVIEW": gmail.get("labels", {}).get("review", "Triage/Review"),
            "CLEANUP": gmail.get("labels", {}).get("cleanup", "Triage/Cleanup"),
        },
        requests_per_second=float(gmail.get("requests_per_second", 4.5)),
        keep_at=float(thresholds.get("keep_at", 0.5)),
        cleanup_below=float(thresholds.get("cleanup_below", 0.15)),
        primary_cleanup_below=float(thresholds.get("primary_cleanup_below", 0.05)),
        trash_after_days=float(thresholds.get("trash_after_days", 7)),
        always_clean=[s.lower() for s in raw.get("always_clean") or []],
        keep_questions=_questions(questions.get("keep"), cleanup=False),
        cleanup_questions=_questions(questions.get("cleanup"), cleanup=True),
    )

    if not cfg.keep_questions:
        raise ValueError("config needs at least one question under questions.keep")
    if "bulk" not in cfg.queries:
        raise ValueError("config needs gmail.queries.bulk")
    names = [q.name for q in cfg.all_questions]
    if len(names) != len(set(names)):
        raise ValueError("question names must be unique across keep and cleanup")
    for q in cfg.cleanup_questions:
        if q.unless and q.unless not in {k.name for k in cfg.keep_questions}:
            raise ValueError(f"'{q.name}.unless' names an unknown keep question: {q.unless}")
    return cfg
