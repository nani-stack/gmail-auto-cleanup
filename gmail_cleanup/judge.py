"""Ask the configured model a set of yes/no questions about each email.

Two backends answer the same questions:

- ``jev``  — TypeSafe's hosted System One model, over the network.
- ``laya`` — Convai's open-source System One model, running locally.

Both return a probability per question, so the rules in `rules.py` do not care
which one produced them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .config import Config, Question


def _state(email: dict) -> dict:
    """The only email fields any question sees. Bodies are never read."""
    return {"email": {
        "from": email["from"],
        "subject": email["subject"],
        "preview": email["snippet"],
    }}


class JevBackend:
    """TypeSafe's hosted model. Questions run in parallel within one request."""

    def __init__(self, options: dict[str, Any], questions: list[Question]):
        from typesafe_sdk import Noul, NoulCriteria  # imported lazily so laya users need no SDK

        self.model = options.get("model", "jev-latest")
        self.concurrency = int(options.get("concurrency", 10))
        self.questions = {
            q.name: Noul(
                instructions=q.instructions,
                criteria=NoulCriteria(**q.criteria) if q.criteria else None,
            )
            for q in questions
        }

    def judge(self, emails: list[dict]) -> None:
        asyncio.run(self._judge(emails))

    async def _judge(self, emails: list[dict]) -> None:
        from typesafe_sdk import AsyncTypeSafeClient, TypeSafeError

        sem = asyncio.Semaphore(self.concurrency)
        async with AsyncTypeSafeClient(model=self.model) as client:

            async def one(email: dict) -> None:
                async with sem:
                    try:
                        r = await client.system_one(state=_state(email), questions=self.questions)
                    except TypeSafeError as err:
                        email["error"] = f"{type(err).__name__}: {err}"
                        return
                email["model"] = r.model
                for name in self.questions:
                    email[name] = r.nouls[name].noul

            await asyncio.gather(*(one(e) for e in emails))


class LayaBackend:
    """Convai's open-source model, loaded once and run locally in batches."""

    def __init__(self, options: dict[str, Any], questions: list[Question]):
        import laya

        self.name = options.get("model", "convaiinnovations/laya")
        self.batch_size = int(options.get("batch_size", 32))
        subfolder = options.get("subfolder")
        self.agent = laya.load(self.name, subfolder=subfolder) if subfolder else laya.load(self.name)
        self.questions = {
            q.name: {
                "type": "noul",
                "instructions": q.instructions,
                **({"criteria": q.criteria} if q.criteria else {}),
            }
            for q in questions
        }

    def judge(self, emails: list[dict]) -> None:
        states = [_state(e) for e in emails]
        try:
            results = self.agent.predict_batch(states, self.questions, batch_size=self.batch_size)
        except Exception as err:  # a local model failure applies to the whole batch
            for e in emails:
                e["error"] = f"{type(err).__name__}: {err}"
            return
        for email, result in zip(emails, results):
            email["model"] = self.name
            for name in self.questions:
                email[name] = result["answers"][name]["noul"]


def build(cfg: Config):
    questions = cfg.all_questions
    return JevBackend(cfg.backend_options, questions) if cfg.backend == "jev" \
        else LayaBackend(cfg.backend_options, questions)
