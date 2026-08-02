"""Class triage: route on what KIND of request this is, not on what it costs.

The shipped budget-aware strategy opens on the cheapest rung and climbs one rung
every time a verdict comes back unresolved. That is a cascade, and a cascade pays
for every rung it passes through: when its escalation signal is wrong it buys the
same answer twice. Session 15's own measurement caught it doing exactly that —
three rungs walked, twelve times the economy strategy's cost on that task, and
the wrong answer at the end of it.

This policy makes the opposite bet. Spend a small, bounded amount up front to
decide what kind of request this is, route it once to the rung that class
deserves, and escalate only for the class where a failure is plausibly a
CAPABILITY failure. Two things follow, and both are the point:

* **Triage is not free.** One classification call is bought per task, at the
  cheapest rung, with a single-word answer. It is metered like any other call and
  reported as its own line, because a router that calls a model to decide which
  model to call has to justify that call out of its own savings.

* **Escalation is asymmetric.** ``escalate`` is per class, not global. A lookup
  that fails did not fail for want of a bigger model — it failed on format, or on
  a badly posed question, and buying a frontier answer for it is buying the same
  answer at eight times the price. A computation that fails might genuinely need
  more capability. So the first retries at its own rung and the second climbs.

Nothing here names a model, a provider, a class, a tier or a threshold. The class
list, what each class routes to, whether it may escalate and how many attempts it
gets all live in ``triage.yaml`` beside the other three config files. Adding a
class is a config edit; so is deciding that your lookups should escalate after
all.

The honest limitation, stated where the code is rather than only in a README: a
classifier is a model, so it is wrong sometimes, and when it is wrong this policy
is confidently wrong in one call rather than expensively right in three. The
proof that measures it (``proofs/p8_policy_tradeops.py``) reports every misroute
it can detect and what each one cost.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import config_dir
from .controller import BudgetedGateway, call_site
from .tiers import TierLadder

#: The file this policy reads, in the same directory as tiers/pricing/budgets.
TRIAGE_FILE = "triage.yaml"

#: Role recorded against the classification call, so the ledger and the trace can
#: separate routing overhead from answering spend without parsing node ids.
TRIAGE_ROLE = "triage"


@dataclass(frozen=True)
class TaskClass:
    """One request class: where it routes, and what it may do when it fails."""

    name: str
    tier: str
    escalate: bool
    attempts: int
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "escalate": self.escalate,
            "attempts": self.attempts,
            "description": self.description,
        }


@dataclass(frozen=True)
class TriageDecision:
    """What triage decided for one task, and how much deciding it cost.

    ``source`` is the part a reader should not have to dig for: ``model`` means a
    classification call was bought and its answer was usable, ``unparsable``
    means one was bought and thrown away, and ``error`` means the call itself
    failed. The last two both land on the fallback class, but they are different
    failures and are counted separately.
    """

    task_class: TaskClass
    raw: str
    source: str
    cost: float = 0.0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    provider: str | None = None
    model: str | None = None
    error: str | None = None

    @property
    def fell_back(self) -> bool:
        return self.source != "model"

    def as_dict(self) -> dict[str, Any]:
        return {
            "class": self.task_class.name,
            "tier": self.task_class.tier,
            "escalate": self.task_class.escalate,
            "attempts": self.task_class.attempts,
            "raw": self.raw,
            "source": self.source,
            "fell_back": self.fell_back,
            "cost": self.cost,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "provider": self.provider,
            "model": self.model,
            "error": self.error,
        }


class TriagePolicy:
    """Classify a request, then route it once to the rung its class deserves."""

    def __init__(
        self,
        *,
        classes: dict[str, TaskClass],
        order: tuple[str, ...],
        fallback: str,
        classifier_tier: str,
        classifier_request: dict[str, Any],
        system: str,
        instruction: str,
        max_task_chars: int,
    ) -> None:
        if not classes:
            raise ValueError("triage.yaml defines no classes")
        if fallback not in classes:
            raise ValueError(f"triage.yaml fallback {fallback!r} is not one of {sorted(classes)}")
        self.classes = classes
        self.order = order
        self.fallback = classes[fallback]
        self.classifier_tier = classifier_tier
        self.classifier_request = dict(classifier_request)
        self.system = system
        self.instruction = instruction
        self.max_task_chars = max_task_chars

    # --- loading ---------------------------------------------------------- #

    @classmethod
    def load(cls, directory: str | Path | None = None) -> TriagePolicy:
        resolved = config_dir(directory)
        path = Path(resolved) / TRIAGE_FILE
        if not path.exists():
            raise FileNotFoundError(
                f"{TRIAGE_FILE} not found in {resolved}. This policy is config-driven; "
                f"point --config-dir at a directory that has one."
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a mapping")
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> TriagePolicy:
        raw_classes = data.get("classes") or {}
        if not isinstance(raw_classes, dict):
            raise ValueError("triage.yaml `classes` must be a mapping of name -> settings")
        order = tuple(str(name) for name in (data.get("order") or list(raw_classes)))
        classes: dict[str, TaskClass] = {}
        for name in order:
            row = raw_classes.get(name)
            if row is None:
                raise ValueError(f"triage.yaml order names an undefined class: {name!r}")
            classes[name] = TaskClass(
                name=name,
                tier=str(row["tier"]),
                escalate=bool(row.get("escalate", False)),
                attempts=max(1, int(row.get("attempts", 1))),
                description=str(row.get("description") or "").strip(),
            )
        classifier = data.get("classifier") or {}
        return cls(
            classes=classes,
            order=order,
            fallback=str(data.get("fallback") or order[-1]),
            classifier_tier=str(classifier.get("tier") or ""),
            classifier_request=dict(classifier.get("request") or {}),
            system=str(classifier.get("system") or "").strip(),
            instruction=str(classifier.get("instruction") or "").strip(),
            max_task_chars=int(classifier.get("max_task_chars", 1200)),
        )

    # --- the decision ------------------------------------------------------ #

    def validate(self, ladder: TierLadder) -> None:
        """Fail loudly if a class routes to a rung this ladder does not have.

        A typo here would otherwise resolve to the ladder's default tier and the
        policy would quietly stop being the policy that was configured.
        """
        unknown = {name: c.tier for name, c in self.classes.items() if c.tier not in ladder.names}
        if unknown:
            raise ValueError(
                f"triage.yaml routes to tiers absent from the ladder {list(ladder.names)}: {unknown}"
            )
        if self.classifier_tier and self.classifier_tier not in ladder.names:
            raise ValueError(
                f"triage.yaml classifier.tier {self.classifier_tier!r} is not in {list(ladder.names)}"
            )

    def prompt_for(self, task: str) -> str:
        """The classification prompt. The task is delimited and labelled as data.

        The classifier reads text it did not write, so the request text is fenced
        and the instruction says to classify it rather than follow it. That is a
        reduction in prompt-injection risk, not a removal of it.
        """
        clipped = task[: self.max_task_chars]
        lines = [self.instruction, ""]
        for name in self.order:
            entry = self.classes[name]
            lines.append(f"- {name}: {entry.description}" if entry.description else f"- {name}")
        lines += [
            "",
            "Reply with exactly one of those class names, lowercase, and nothing else.",
            "",
            "<<<REQUEST_TO_CLASSIFY",
            clipped,
            "REQUEST_TO_CLASSIFY",
        ]
        return "\n".join(lines)

    def parse(self, text: str) -> TaskClass | None:
        """Read a class name out of the reply, or ``None`` if it is not in there.

        Strict on purpose. A reply that names two classes, or none, is not a
        weak signal to be coerced into the nearest match — it is an unusable
        answer, and the fallback exists for it.
        """
        cleaned = (text or "").strip().strip("`\"'.").lower()
        if cleaned in self.classes:
            return self.classes[cleaned]
        words = {word.strip(",.;:!?()[]\"'`") for word in cleaned.split()}
        hits = [name for name in self.order if name in words]
        return self.classes[hits[0]] if len(hits) == 1 else None

    async def classify(
        self,
        controller: BudgetedGateway,
        *,
        task: str,
        node_id: str,
        overrides: dict[str, Any] | None = None,
    ) -> TriageDecision:
        """Buy one classification, metered like any other call.

        Goes through the same :class:`BudgetedGateway` as the answer, so the
        routing decision is admitted, priced and charged against the same run
        ceiling. A router that spent off-budget would not be a router this
        session could talk about.
        """
        request = {**self.classifier_request, **(overrides or {})}
        before = len(controller.budget.charges)
        with call_site(node_id, TRIAGE_ROLE, self.classifier_tier or None):
            try:
                reply = await controller.complete(
                    self.prompt_for(task), self.system, request=request or None
                )
            except Exception as error:  # a refusal, a rate limit, a dead provider
                charge = self._charge_after(controller, before)
                return TriageDecision(
                    task_class=self.fallback, raw="", source="error",
                    error=f"{type(error).__name__}: {error}", **charge,
                )
        charge = self._charge_after(controller, before)
        text = str(reply.get("text") or "")
        chosen = self.parse(text)
        return TriageDecision(
            task_class=chosen or self.fallback,
            raw=text.strip()[:120],
            source="model" if chosen else self._why_unusable(text, charge, request),
            provider=reply.get("provider"),
            model=reply.get("model"),
            **charge,
        )

    @staticmethod
    def _why_unusable(text: str, charge: dict[str, Any], request: dict[str, Any]) -> str:
        """Separate "the model said something I can't read" from "it said nothing".

        An empty reply that used its entire output allowance is not an unparsable
        answer, it is a TRUNCATED one: the tokens were spent in a channel the
        gateway does not read, and the fix is a bigger ceiling rather than a
        better parser. Measured here on 2026-08-02 with an 8-token ceiling, which
        returned "" on every task at full price. Naming the two cases apart is
        what turned that from a mystery into a one-line config change.
        """
        ceiling = int(request.get("max_tokens") or 0)
        if not text.strip() and ceiling and charge.get("output_tokens", 0) >= ceiling:
            return "truncated"
        return "unparsable"

    @staticmethod
    def _charge_after(controller: BudgetedGateway, before: int) -> dict[str, Any]:
        """Whatever the ledger recorded for this call — zero if it never landed."""
        charges = controller.budget.charges[before:]
        return {
            "cost": sum(c.cost for c in charges),
            "calls": len(charges),
            "input_tokens": sum(c.input_tokens for c in charges),
            "output_tokens": sum(c.output_tokens for c in charges),
            "latency_ms": sum(c.latency_ms or 0.0 for c in charges),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "classes": {name: self.classes[name].as_dict() for name in self.order},
            "fallback": self.fallback.name,
            "classifier_tier": self.classifier_tier,
            "classifier_request": self.classifier_request,
        }


def triage_config_path(directory: str | Path | None = None) -> Path:
    return Path(config_dir(directory)) / TRIAGE_FILE


def triage_enabled(directory: str | Path | None = None) -> bool:
    """Whether a triage config is present at all. Absent means "not this run"."""
    if os.getenv("S15_TRIAGE_DISABLED") == "1":
        return False
    return triage_config_path(directory).exists()


__all__ = [
    "TRIAGE_FILE",
    "TRIAGE_ROLE",
    "TaskClass",
    "TriageDecision",
    "TriagePolicy",
    "triage_config_path",
    "triage_enabled",
]
