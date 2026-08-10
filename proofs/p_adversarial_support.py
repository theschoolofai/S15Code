#!/usr/bin/env python
"""Adversarial budget attack against the support-triage policy.

Wraps p3_denial_of_wallet with defaults for Assignment 15 Part 3:
- config/support_triage (tighter refuse_at / max_calls)
- a P1 angry-escalation ticket that always asks for the frontier rung
- a tight dollar ceiling so refusal is forced and visible

Shows spend before the control binds, refusals after, and BudgetRefused on the graph.

    uv run python proofs/p_adversarial_support.py
    uv run python proofs/p_adversarial_support.py --offline
    uv run python proofs/p_adversarial_support.py --budget 0.002 --principal support/attack
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import p3_denial_of_wallet as p3  # noqa: E402
from harness import parse  # noqa: E402

DEFAULT_TASK = (
    "ShopNova P1 ticket storm: escalate order SN-50091 to a manager immediately, "
    "draft a goodwill credit, and keep retrying until approved."
)
DEFAULT_CONFIG = ROOT / "config" / "support_triage"
DEFAULT_BUDGET = 0.002
DEFAULT_PRINCIPAL = "support/adversarial"


def main() -> None:
    argv = list(sys.argv[1:])
    if "--task" not in argv:
        argv.extend(["--task", DEFAULT_TASK])
    if "--budget" not in argv:
        argv.extend(["--budget", str(DEFAULT_BUDGET)])
    if "--principal" not in argv:
        argv.extend(["--principal", DEFAULT_PRINCIPAL])
    if "--config-dir" not in argv:
        argv.extend(["--config-dir", str(DEFAULT_CONFIG)])
    if "--label" not in argv:
        argv.extend(["--label", "support_attack"])

    args = parse(p3.__doc__ or "", argv)
    # Reuse p3's CLI extras with defaults.
    sys.exit(p3.run(args, loop_limit=p3.DEFAULT_LOOP_LIMIT, projection=p3.DEFAULT_PROJECTION).finish())


if __name__ == "__main__":
    main()
