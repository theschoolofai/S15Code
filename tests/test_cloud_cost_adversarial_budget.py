"""Adversarial attack on the cloud_cost_ladder budget policy.

Frames a free-tier agent that keeps refining forever: the planner asks for the
dearest rung on every iteration. Without the controller, projected spend grows
with the loop; with it, spend stays under the ceiling and refusals are visible
in the ledger and in OTEL (budget.refused on the run span).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proofs"))

from harness import Args, sync  # noqa: E402
from p3_denial_of_wallet import drive_runaway  # noqa: E402

from s15code.economics import EconomicsConfig  # noqa: E402
from s15code.telemetry import SPAN_KIND, export_run  # noqa: E402

CONFIG_DIR = ROOT / "config" / "cloud_cost_ladder"
PROJECTION_ROUNDS = 10_000


def _cloud_args(*, budget: float = 0.002) -> Args:
    return Args(
        task="Keep refining the free-tier cost comparison forever.",
        budget=budget,
        principal="proofs/s15/cloud_cost/adversary",
        offline=True,
        base_url="http://127.0.0.1:9",
        otel_endpoint=None,
        respond_as="text",
        config_dir=str(CONFIG_DIR),
        live_embeddings=False,
        label="cloud_cost_adversary",
    )


def test_cloud_cost_ladder_thresholds():
    config = EconomicsConfig.load(CONFIG_DIR)
    assert list(config.ladder.names) == ["economy", "standard", "frontier"]
    assert config.thresholds.max_calls_per_run == 24
    assert config.thresholds.refuse_at == 0.85
    assert config.thresholds.downgrade_at == 0.40


def test_uncontrolled_projection_exceeds_ceiling_then_controller_refuses(tmp_path):
    """Spend-before vs refuse-after: the assignment's Part 3 shape."""
    args = _cloud_args(budget=0.002)
    outcome = sync(drive_runaway(args, loop_limit=80, data_dir=tmp_path))
    budget = outcome["budget"]

    cost_per_call = budget["spent"] / budget["calls"] if budget["calls"] else 0.0
    uncontrolled = cost_per_call * PROJECTION_ROUNDS

    assert budget["spent"] <= budget["total"]
    assert budget["refusals"] > 0
    assert outcome["refused_nodes"] > 0
    assert uncontrolled > budget["total"]
    assert budget["calls"] < outcome["rounds"]

    export = export_run(
        {
            "run_id": "runaway",
            "finished": outcome["finished"],
            "nodes": {},
            "edges": (),
            "events": outcome["journal"],
        },
        budget=budget,
    )
    run_span = next(s for s in export.spans if dict(s.attributes).get(SPAN_KIND) == "run")
    assert dict(run_span.attributes)["s15.budget.refusals"] >= 1
    assert "budget.refused" in [event.name for event in run_span.events]


def test_refusal_event_on_run_span_matches_telemetry_contract():
    """Same contract as tests/test_telemetry.py for refusals."""
    export = export_run(
        {
            "run_id": "cloud-adv",
            "finished": False,
            "nodes": {},
            "edges": (),
            "events": [
                {"sequence": 1, "kind": "run_started", "node_id": None, "payload": {}},
            ],
        },
        budget={
            "currency": "USD",
            "total": 0.002,
            "spent": 0.002,
            "remaining": 0.0,
            "refusals": 3,
            "refusal_log": [
                {
                    "node_id": "loop_4",
                    "reason": "spend pressure >= refuse_at (cloud_cost_ladder)",
                    "spent": 0.002,
                    "remaining": 0.0,
                }
            ],
        },
    )
    run_span = next(s for s in export.spans if dict(s.attributes).get(SPAN_KIND) == "run")
    assert dict(run_span.attributes)["s15.budget.refusals"] == 3
    assert "budget.refused" in [e.name for e in run_span.events]
