"""Tests for the code-review policy in ``config/part2``.

These are not tests of the library — the library has its own suite. They assert
that THIS POLICY says what its author thinks it says, which is a different and
surprisingly easy thing to get wrong.

The first test exists because it failed. An earlier draft of
``config/part2/budgets.yaml`` nested every threshold under a ``thresholds:``
block. ``PolicyThresholds.from_mapping()`` reads them from the TOP level, so all
eight were silently discarded in favour of library defaults — including both call
ceilings, which meant they were disabled for an entire measurement run. There was
no error and no warning. A config file that looks configured and is not is worth
a test.

Nothing here contacts a provider.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from s15code.economics import EconomicsConfig
from s15code.evals import EvalsConfig

CONFIGS = ["config/part2", "config/part2_frontier_fair", "config/part2_descend"]
ROOT = Path(__file__).resolve().parents[1]


def econ(directory: str) -> EconomicsConfig:
    return EconomicsConfig.load(str(ROOT / directory))


def evals(directory: str) -> EvalsConfig:
    return EvalsConfig.load(str(ROOT / directory))


# --------------------------------------------------------------------------- #
# the bug that motivated this file
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("directory", CONFIGS)
def test_thresholds_are_actually_loaded_not_silently_defaulted(directory: str) -> None:
    """Every threshold written in budgets.yaml must survive the loader.

    Compares what the YAML says against what the loader produced. A nested
    ``thresholds:`` block passes YAML parsing and then vanishes, so asserting on
    the loaded object is the only check that catches it.
    """
    raw = yaml.safe_load((ROOT / directory / "budgets.yaml").read_text())
    loaded = econ(directory).thresholds

    for key in ("downgrade_at", "refuse_at", "headroom_fraction", "reserve_fraction",
                "max_calls_per_run", "max_calls_per_node",
                "chars_per_token", "input_estimate_safety"):
        assert key in raw, (
            f"{directory}/budgets.yaml is missing {key!r} at the TOP level. "
            "Thresholds nested under a `thresholds:` key are silently ignored."
        )
        assert raw[key] == pytest.approx(getattr(loaded, key)), (
            f"{directory}: budgets.yaml says {key}={raw[key]!r} but the loader "
            f"produced {getattr(loaded, key)!r} — the key is not being read."
        )


@pytest.mark.parametrize("directory", CONFIGS)
def test_call_ceilings_are_armed(directory: str) -> None:
    """Zero means unlimited. A budget policy that means to bound a loop must say so."""
    t = econ(directory).thresholds
    assert t.max_calls_per_run > 0, f"{directory}: max_calls_per_run is unlimited"
    assert t.max_calls_per_node > 0, f"{directory}: max_calls_per_node is unlimited"
    assert t.max_calls_per_node <= t.max_calls_per_run


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("directory", CONFIGS)
def test_every_rung_is_priced_by_its_own_row(directory: str) -> None:
    """An unpriced model must not silently cost the default rate.

    ``config/part2/pricing.yaml`` sets a deliberately punitive default (10/40) so
    a missing row is loud rather than flattering. This asserts no rung relies on it.
    """
    config = econ(directory)
    raw = yaml.safe_load((ROOT / directory / "pricing.yaml").read_text())
    for name in config.ladder._order:
        tier = config.ladder._tiers[name]
        assert tier.price_model in raw["models"], (
            f"{directory}: rung {name!r} prices against {tier.price_model!r}, "
            "which has no row in pricing.yaml and would fall through to `default`."
        )


@pytest.mark.parametrize("directory", CONFIGS)
def test_projected_cost_is_monotone_up_the_ladder(directory: str) -> None:
    """"One rung down" is only meaningful if cost rises with the order."""
    config = econ(directory)
    projected = []
    for name in config.ladder._order:
        tier = config.ladder._tiers[name]
        price = config.pricing.price_for(tier.price_model)
        # Admission prices max_tokens for output, not the projection estimate.
        worst = (400 * price.input + tier.request["max_tokens"] * price.output) / 1e6
        projected.append((name, worst))
    costs = [c for _, c in projected]
    assert costs == sorted(costs), f"{directory}: not monotone — {projected}"
    assert costs[0] > 0, (
        f"{directory}: the cheapest rung costs $0. A free rung cannot be exhausted "
        "by a dollar ceiling, so refuse-at-exhaustion stops being expressible."
    )


@pytest.mark.parametrize("directory", CONFIGS)
def test_rungs_are_distinct_models(directory: str) -> None:
    """A ladder whose rungs share a model varies token budgets, not capability."""
    config = econ(directory)
    models = [config.ladder._tiers[n].request["model"] for n in config.ladder._order]
    assert len(set(models)) == len(models), f"{directory}: duplicate models {models}"


@pytest.mark.parametrize("directory", CONFIGS)
def test_reasoning_is_disabled_on_thinking_by_default_models(directory: str) -> None:
    """gpt-oss-120b spends its whole output budget thinking and returns "" at
    full price unless the dial is explicitly off. Not decoration."""
    config = econ(directory)
    for name in config.ladder._order:
        request = config.ladder._tiers[name].request
        if "gpt-oss" in str(request.get("model", "")):
            assert request.get("reasoning") == "off", (
                f"{directory}: rung {name!r} uses {request['model']} without "
                "reasoning:off — it will bill a full response and return nothing."
            )


# --------------------------------------------------------------------------- #
# the budget
# --------------------------------------------------------------------------- #
def test_part2_ceiling_affords_a_full_climb() -> None:
    """config/part2 is deliberately sized so escalation is always affordable, so
    that any quality loss is attributable to ROUTING rather than to exhaustion."""
    config = econ("config/part2")
    total = 0.0
    for name in config.ladder._order:
        tier = config.ladder._tiers[name]
        price = config.pricing.price_for(tier.price_model)
        total += (400 * price.input + tier.request["max_tokens"] * price.output) / 1e6
    assert config.default_budget > total, (
        f"ceiling {config.default_budget} does not cover a full three-rung climb "
        f"({total:.6f}); quality loss would confound routing with exhaustion."
    )


def test_descend_ceiling_cannot_afford_the_top_rung() -> None:
    """config/part2_descend exists to show that the CEILING, not the config,
    decides which rungs are reachable. If the top rung became affordable the
    variant would silently degenerate into the always-frontier baseline."""
    config = econ("config/part2_descend")
    top = config.ladder.most_capable
    price = config.pricing.price_for(top.price_model)
    worst = (400 * price.input + top.request["max_tokens"] * price.output) / 1e6
    assert worst > config.default_budget, (
        f"descend ceiling {config.default_budget} can afford the top rung "
        f"({worst:.6f}); the variant no longer demonstrates anything."
    )


# --------------------------------------------------------------------------- #
# the judge
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("directory", CONFIGS)
def test_no_judge_can_grade_its_own_models_output(directory: str) -> None:
    """Panel and ladder must be disjoint by MODEL, not merely by provider."""
    config, rubric = econ(directory), evals(directory).rubric
    ladder = {config.ladder._tiers[n].request["model"] for n in config.ladder._order}
    panel = {m.request["model"] for m in rubric.panel}
    assert not (ladder & panel), (
        f"{directory}: {ladder & panel} appears in both the ladder and the judge "
        "panel, so a model could grade its own output."
    )


@pytest.mark.parametrize("directory", CONFIGS)
def test_panel_has_at_least_two_members(directory: str) -> None:
    """One judge cannot disagree with itself, so agreement becomes unmeasurable."""
    assert len(evals(directory).rubric.panel) >= 2, f"{directory}: single judge"


@pytest.mark.parametrize("directory", CONFIGS)
def test_the_floored_criteria_are_the_ones_with_an_expectation(directory: str) -> None:
    """A KNOWN DEFECT, asserted so it is not mistaken for intent.

    `min_criterion` applies to every criterion, including `no_false_alarms` at
    weight 1 — so a 1-of-3 there vetoes an otherwise perfect answer. Measured:
    `h04_retry_budget` scores 0.889-0.944, above the 0.8 bar, and fails 10/10
    (proofs/part2_judge_noise.py).

    The rubric is left as measured rather than tuned after the fact, so this test
    documents the shape rather than asserting the fix. Change the floor to apply
    only to `requires_expectation` criteria and this test should be inverted.
    """
    rubric = evals(directory).rubric
    weights = {c.name: c.weight for c in rubric.criteria}
    assert weights.get("no_false_alarms", 0) < weights.get("identifies_defect", 0), (
        "no_false_alarms must carry less weight than identifies_defect"
    )
    assert rubric.min_criterion > 0, (
        "min_criterion is 0, so a wrong diagnosis could be rescued by a good fix"
    )
    # The defect: a weight-1 criterion has the same veto power as a weight-3 one.
    assert rubric.min_criterion > 0 and weights["no_false_alarms"] == 1.0, (
        "if no_false_alarms is no longer weight 1, revisit the floor analysis in "
        "PART2_RESULTS.md section 7.2"
    )


@pytest.mark.parametrize("directory", CONFIGS)
def test_rubric_scores_no_criterion_for_fluency(directory: str) -> None:
    """On a task with a right answer, rewarding prose measures writing skill,
    correlates with model size, and hands the dear rung an advantage unrelated to
    finding the bug. Omitting it is what stops the rubric assuming its conclusion."""
    banned = ("clear", "clarity", "well_written", "helpful", "readable", "style", "tone")
    for criterion in evals(directory).rubric.criteria:
        assert not any(word in criterion.name.lower() for word in banned), (
            f"{directory}: criterion {criterion.name!r} scores presentation"
        )


# --------------------------------------------------------------------------- #
# the task set
# --------------------------------------------------------------------------- #
def test_task_set_has_a_real_difficulty_gradient() -> None:
    from s15code.evals import load_tasks

    tasks = load_tasks(str(ROOT / "proofs" / "tasks" / "codereview.jsonl"))
    assert len(tasks) >= 15, f"the brief requires 15+ tasks, found {len(tasks)}"
    by_difficulty: dict[str, int] = {}
    for task in tasks:
        by_difficulty[task.difficulty or "unlabelled"] = (
            by_difficulty.get(task.difficulty or "unlabelled", 0) + 1
        )
    assert set(by_difficulty) == {"easy", "moderate", "hard"}, by_difficulty
    assert all(count >= 5 for count in by_difficulty.values()), by_difficulty


def test_every_task_states_its_success_criterion() -> None:
    """The judge grades against `expectation`. A task without one is scored on
    generic criteria alone, which cannot distinguish the planted defect from a
    plausible neighbouring one."""
    from s15code.evals import load_tasks

    tasks = load_tasks(str(ROOT / "proofs" / "tasks" / "codereview.jsonl"))
    missing = [t.id for t in tasks if not (t.expectation or "").strip()]
    assert not missing, f"tasks with no expectation: {missing}"


def test_hard_tasks_name_their_decoy() -> None:
    """Every hard task must tell the judge what the plausible WRONG answer is,
    or a confident near-miss scores as partial credit instead of a miss."""
    from s15code.evals import load_tasks

    tasks = load_tasks(str(ROOT / "proofs" / "tasks" / "codereview.jsonl"))
    for task in (t for t in tasks if t.difficulty == "hard"):
        text = (task.expectation or "").lower()
        assert ("not" in text and ("missed" in text or "have not" in text
                                   or "do not" in text or "no not" in text)) or "decoy" in text, (
            f"{task.id}: expectation does not name a wrong answer to reject"
        )
