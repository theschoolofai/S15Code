"""Regression checks for the Session 15 support-triage routing policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from s15code.economics import EconomicsConfig
from s15code.evals import EvalsConfig, load_tasks

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "proofs" / "config" / "support_triage"
TASKS = ROOT / "proofs" / "tasks" / "support_triage.jsonl"


def test_support_triage_profile_declares_a_budget_ladder_not_python_policy() -> None:
    config = EconomicsConfig.load(CONFIG)
    evals = EvalsConfig.load(CONFIG)

    assert config.ladder.names == ("intake", "analyst", "incident_commander")
    assert config.ladder.for_role("answer_with_evidence").name == "incident_commander"
    assert evals.strategies.start == "cheapest"
    assert evals.strategies.escalate is True

    models = [config.ladder.tier(name).model for name in config.ladder.names]
    providers = [config.ladder.tier(name).request.get("provider") for name in config.ladder.names]
    assert len(set(models)) == len(models)
    assert len(set(providers)) >= 2

    projections = [config.policy().project(config.ladder.tier(name)) for name in config.ladder.names]
    assert projections == sorted(projections)
    assert projections[0] < projections[-1]

    assert config.ceiling_for("demo/support-triage/adversary", requested=1.0) == pytest.approx(0.003)


def test_support_triage_workload_has_fifteen_labelled_tasks() -> None:
    tasks = load_tasks(TASKS)
    difficulties = {task.difficulty for task in tasks}

    assert len(tasks) >= 15
    assert {"routine", "diagnostic", "escalation"} <= difficulties
    assert all(task.expectation for task in tasks)


def test_support_triage_call_ceiling_refuses_a_runaway_loop_before_more_spend() -> None:
    config = EconomicsConfig.load(CONFIG)
    budget = config.budget(principal="demo/support-triage/adversary", amount=1.0, run_id="attack")
    budget.charges = [None] * config.thresholds.max_calls_per_run  # type: ignore[list-item]

    decision = config.policy().decide(
        node_id="loop_999",
        requested_tier=config.ladder.most_capable.name,
        budget=budget,
        input_tokens=600,
    )

    assert decision.action == "refuse"
    assert "run call ceiling" in decision.reason
