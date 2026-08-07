#!/usr/bin/env python
"""Custom proof: SQL generation policy with cost measurement.

Tests a budget-aware routing policy for SQL generation tasks against an
always-frontier baseline. Measures cost per call and cost per resolved task.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import OUT, Args, Proof, sync

from s15code.economics import (
    BudgetedGateway,
    BudgetRefused,
    EconomicsConfig,
    MeteredTransport,
    TierLadder,
    call_site,
)
from s15code.evals import EvalsConfig, RubricJudge, Verdict, load_tasks
from s15code.evals.judge import STATUS_JUDGE_FAILED
from s15code.evals.tasks import EvalTask
from s15code.gateway import GatewayClient

DEFAULT_BASE_URL = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8112")
DEFAULT_TASKS = Path(__file__).resolve().parent / "tasks" / "sql_generation.jsonl"

ANSWER_ROLE = "sql_generator"
ANSWER_SYSTEM = (
    "You are a SQL expert. Generate correct, efficient SQL queries based on the user's request. "
    "Return ONLY the SQL query, with no explanation or surrounding text unless the task explicitly asks for it. "
    "Use standard SQL syntax that works across most database systems unless the task specifies a particular dialect."
)


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    start_tier: str
    attempts: int
    escalate: bool
    description: str

    def next_tier(self, ladder: TierLadder, current: str) -> str:
        if not self.escalate:
            return current
        names = ladder.names
        index = names.index(current) if current in names else 0
        return names[min(index + 1, len(names) - 1)]


def strategies_for(ladder: TierLadder) -> dict[str, Strategy]:
    top = ladder.most_capable.name
    bottom = ladder.cheapest.name
    return {
        "A": Strategy("A", "always_frontier", top, 1, False,
                      f"every task once on {top}"),
        "B": Strategy("B", "budget_aware_economy", bottom, 3, True,
                      f"starts on {bottom}, escalates up to 3 attempts"),
    }


class SimulatedGateway:
    simulated = True

    def __init__(self, *, latency_ms: float = 4.0) -> None:
        self.latency_ms = latency_ms

    @staticmethod
    def _needed_tokens(prompt: str) -> int:
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
        return 200 + int(digest, 16) % 3600

    async def chat(self, *, prompt: str, system: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = dict(request or {})
        ceiling = int(request.get("max_tokens") or 512)
        model = request.get("model") or "simulated-model"
        response_format = request.get("response_format")

        if response_format:
            names = (
                response_format.get("schema", {}).get("properties", {})
                .get("scores", {}).get("required", [])
            )
            match = re.search(r"sufficient=(\d)", prompt)
            good = bool(match and match.group(1) == "1")
            top = 4 if good else 1
            body = json.dumps({"scores": {name: top for name in names},
                               "notes": "simulated verdict"})
            return {"text": body, "provider": "simulated", "model": model,
                    "input_tokens": len(prompt) // 4, "output_tokens": len(body) // 4,
                    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                    "latency_ms": self.latency_ms}

        needed = self._needed_tokens(prompt)
        sufficient = int(ceiling >= needed)
        text = (
            f"SIMULATED_SQL sufficient={sufficient} ceiling={ceiling} needed={needed}. "
            "SELECT * FROM simulated_table WHERE id = 1;"
        )
        return {"text": text, "provider": "simulated", "model": model,
                "input_tokens": len(prompt) // 4 + len(system) // 4,
                "output_tokens": min(needed, ceiling),
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                "latency_ms": self.latency_ms}


def gateway_reachable(base_url: str, *, timeout: float = 3.0) -> bool:
    try:
        return httpx.get(f"{base_url}/healthz", timeout=timeout).status_code == 200
    except Exception:
        return False


@dataclass
class Judged:
    verdict: Verdict
    cached: bool


@dataclass
class JudgeDesk:
    judge: RubricJudge
    verdicts: dict[tuple[str, str], Verdict] = field(default_factory=dict)
    reuses: int = 0

    async def verdict_for(
        self, task: EvalTask, *, answer: str, provider: str | None, model: str | None, error: str | None
    ) -> Judged:
        digest = hashlib.sha256((answer or "").encode("utf-8")).hexdigest()
        key = (task.id, f"{error}|{digest}")
        if key in self.verdicts:
            self.reuses += 1
            return Judged(self.verdicts[key], True)
        verdict = await self.judge.judge(
            task=task.task, answer=answer, expectation=task.expectation, task_id=task.id,
            answer_provider=provider, answer_model=model, error=error,
        )
        self.verdicts[key] = verdict
        return Judged(verdict, False)


async def run_one(
    *,
    task: EvalTask,
    strategy: Strategy,
    transport: MeteredTransport,
    config: EconomicsConfig,
    desk: JudgeDesk,
    principal: str,
    ceiling: float,
    session: str,
) -> dict[str, Any]:
    budget = config.budget(principal=principal, amount=ceiling, run_id=f"sql-{strategy.key}-{task.id}")
    controller = BudgetedGateway(
        transport, budget=budget, policy=config.policy(), pricing=config.pricing, ladder=config.ladder
    )
    overrides = {"agent": f"sql_{strategy.name}", "session": session}

    tier = strategy.start_tier
    attempts: list[dict[str, Any]] = []
    final: Verdict | None = None
    for index in range(strategy.attempts):
        node_id = f"{task.id}#{strategy.key}{index + 1}"
        answer, error, reply = "", None, {}
        started = time.time()
        with call_site(node_id, ANSWER_ROLE, tier):
            try:
                reply = await controller.complete(task.task, ANSWER_SYSTEM, request=overrides)
                answer = str(reply.get("text") or "")
            except BudgetRefused as refused:
                error = f"budget refused: {refused}"
            except Exception as failure:
                error = f"{type(failure).__name__}: {failure}"
        elapsed = (time.time() - started) * 1000.0
        charge = budget.charges[-1].as_dict() if budget.charges and not error else {}

        judged = await desk.verdict_for(
            task, answer=answer, provider=reply.get("provider"), model=reply.get("model"), error=error
        )
        final = judged.verdict
        attempts.append({
            "attempt": index + 1,
            "node_id": node_id,
            "requested_tier": tier,
            "charged_tier": charge.get("tier"),
            "decision": charge.get("decision"),
            "provider": reply.get("provider"),
            "model": reply.get("model"),
            "cost": charge.get("cost", 0.0),
            "input_tokens": charge.get("input_tokens", 0),
            "output_tokens": charge.get("output_tokens", 0),
            "latency_ms": charge.get("latency_ms", elapsed),
            "error": error,
            "answer_chars": len(answer),
            "answer_excerpt": answer[-320:],
            "verdict": {
                "status": judged.verdict.status,
                "resolved": judged.verdict.resolved,
                "overall": judged.verdict.overall,
                "agreement": judged.verdict.agreement,
                "disputed": judged.verdict.disputed,
                "reason": judged.verdict.reason,
                "judged_by": list(judged.verdict.judged_by),
                "self_judged": judged.verdict.self_judged,
                "reused": judged.cached,
            },
        })
        if judged.verdict.resolved:
            break
        if judged.verdict.failed:
            break
        tier = strategy.next_tier(config.ladder, tier)

    snapshot = budget.snapshot()
    return {
        "task_id": task.id,
        "difficulty": task.difficulty,
        "strategy": strategy.key,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "resolved": bool(final and final.resolved),
        "status": final.status if final else "not_run",
        "overall": final.overall if final else None,
        "agreement": final.agreement if final else None,
        "self_judged": bool(final and final.self_judged),
        "cost": snapshot["spent"],
        "calls": snapshot["calls"],
        "input_tokens": sum(charge["input_tokens"] for charge in snapshot["charges"]),
        "output_tokens": sum(charge["output_tokens"] for charge in snapshot["charges"]),
        "latency_ms": sum(charge["latency_ms"] or 0.0 for charge in snapshot["charges"]),
        "tiers_charged": [charge["tier"] for charge in snapshot["charges"]],
        "models": sorted({charge["model"] for charge in snapshot["charges"] if charge["model"]}),
        "downgrades": snapshot["downgrades"],
        "branches": snapshot["branches"],
        "refusals": snapshot["refusals"],
        "verdict": final.as_dict() if final else None,
    }


def summarise(rows: list[dict[str, Any]], strategy: Strategy) -> dict[str, Any]:
    spend = sum(row["cost"] for row in rows)
    calls = sum(row["calls"] for row in rows)
    resolved = sum(1 for row in rows if row["resolved"])
    tokens = sum(row["input_tokens"] + row["output_tokens"] for row in rows)
    return {
        "strategy": strategy.key,
        "name": strategy.name,
        "description": strategy.description,
        "start_tier": strategy.start_tier,
        "max_attempts": strategy.attempts,
        "escalates": strategy.escalate,
        "tasks": len(rows),
        "spend": spend,
        "calls": calls,
        "attempts": sum(row["attempt_count"] for row in rows),
        "cost_per_call": (spend / calls) if calls else None,
        "cost_per_task": (spend / len(rows)) if rows else None,
        "resolved": resolved,
        "unresolved": sum(1 for row in rows if row["status"] == "unresolved"),
        "judge_failed": sum(1 for row in rows if row["status"] == STATUS_JUDGE_FAILED),
        "cost_per_resolved_task": (spend / resolved) if resolved else None,
        "resolution_rate": (resolved / len(rows)) if rows else None,
        "tokens": tokens,
        "tokens_per_resolved_task": (tokens / resolved) if resolved else None,
        "latency_ms": sum(row["latency_ms"] for row in rows),
        "errors": sum(1 for row in rows for attempt in row["attempts"] if attempt["error"]),
        "downgrades": sum(row["downgrades"] for row in rows),
        "branches": sum(row["branches"] for row in rows),
        "refusals": sum(row["refusals"] for row in rows),
        "tiers_charged": sorted({tier for row in rows for tier in row["tiers_charged"]}),
        "models": sorted({model for row in rows for model in row["models"]}),
        "resolved_by_difficulty": {
            label: sum(1 for row in rows if (row["difficulty"] or "unlabelled") == label and row["resolved"])
            for label in sorted({row["difficulty"] or "unlabelled" for row in rows})
        },
    }


def break_even_resolution_rate(
    *, cheap_cost_per_call: float | None, dear_cost_per_resolved: float | None, attempts: int
) -> float | None:
    if not cheap_cost_per_call or not dear_cost_per_resolved or attempts < 1:
        return None
    denominator = dear_cost_per_resolved / cheap_cost_per_call - 1 + attempts
    if denominator <= 0:
        return None
    return min(1.0, attempts / denominator)


async def _measure(
    *, tasks: tuple[EvalTask, ...], chosen: list[Strategy], transport: MeteredTransport,
    config: EconomicsConfig, desk: JudgeDesk, principal: str, ceiling: float, session: str,
) -> dict[str, list[dict[str, Any]]]:
    results: dict[str, list[dict[str, Any]]] = {}
    for strategy in chosen:
        rows = []
        for task in tasks:
            rows.append(await run_one(
                task=task, strategy=strategy, transport=transport, config=config, desk=desk,
                principal=principal, ceiling=ceiling, session=session,
            ))
            print(f"  {strategy.key} {task.id:18} {rows[-1]['status']:12} "
                  f"attempts={rows[-1]['attempt_count']} cost={rows[-1]['cost']:.8f} "
                  f"tiers={','.join(rows[-1]['tiers_charged']) or '-'}", flush=True)
        results[strategy.key] = rows
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS))
    parser.add_argument("--principal", default="sql/agent/policy")
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--strategies", default="A,B")
    parser.add_argument("--judge-provider", default=None)
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--label", default="", help="suffix for the JSON written to proofs/out/")
    return parser.parse_args(argv)


def run(parsed: argparse.Namespace) -> Proof:
    config_dir = parsed.config_dir or str(ROOT / "config")
    config = EconomicsConfig.load(config_dir)
    evals = EvalsConfig.load(config_dir)
    tasks = load_tasks(parsed.tasks)
    ceiling = config.ceiling_for(
        parsed.principal, parsed.budget if parsed.budget is not None else config.default_budget
    )

    args = Args(
        task=f"{len(tasks)} SQL tasks from {parsed.tasks}", budget=ceiling, principal=parsed.principal,
        offline=parsed.offline, base_url=parsed.base_url.rstrip("/"), otel_endpoint=None,
        respond_as="text", config_dir=config_dir, live_embeddings=False, label=parsed.label,
    )

    available = strategies_for(config.ladder)
    wanted = [key.strip().upper() for key in parsed.strategies.split(",") if key.strip()]
    unknown = [key for key in wanted if key not in available]
    if unknown:
        raise SystemExit(f"unknown strategies {unknown}; choose from {sorted(available)}")
    chosen = [available[key] for key in wanted]

    live = not parsed.offline and gateway_reachable(args.base_url)
    if live:
        client: Any = GatewayClient(args.base_url)
        mode, detail = "live", {"base_url": args.base_url}
    else:
        client = SimulatedGateway()
        mode = "offline"
        detail = {"reason": "--offline requested" if parsed.offline else f"{args.base_url} unreachable",
                  "simulated": True,
                  "warning": "offline numbers are a deterministic simulation, NOT evidence"}
    transport = MeteredTransport(client)

    panel = evals.rubric.panel
    if parsed.judge_provider:
        from s15code.evals import JudgeModel
        request = dict(panel[0].request) if panel else {}
        request["provider"] = parsed.judge_provider
        if parsed.judge_model:
            request["model"] = parsed.judge_model
        else:
            request.pop("model", None)
        panel = (JudgeModel(name=f"cli_{parsed.judge_provider}", request=request),)
    desk = JudgeDesk(RubricJudge(client, evals.rubric, pricing=config.pricing, panel=panel))

    proof = Proof(name="p_sql_policy", args=args, mode=mode, mode_detail=detail)

    print(f"\np_sql: {len(tasks)} tasks x {len(chosen)} strategies, {mode} mode")
    print(f"  ladder   {' < '.join(config.ladder.names)}")
    models = sorted(m for m in {config.ladder.tier(name).model for name in config.ladder.names} if m)
    print(f"  models   {models}")
    print(f"  judges   {[f'{m.provider}/{m.model}' for m in panel]}")
    print()

    started = time.time()
    results = sync(_measure(
        tasks=tasks, chosen=chosen, transport=transport, config=config, desk=desk,
        principal=parsed.principal, ceiling=ceiling, session=parsed.label or "sql",
    ))
    elapsed = time.time() - started

    summaries = {key: summarise(rows, available[key]) for key, rows in results.items()}

    currency = config.pricing.currency
    for key, row in summaries.items():
        per_resolved = row["cost_per_resolved_task"]
        proof.fact(f"{key} {row['name']}", (
            f"spend {row['spend']:.8f} {currency}  calls {row['calls']}  "
            f"cost/call {row['cost_per_call'] or 0:.8f}  resolved {row['resolved']}/{row['tasks']}  "
            f"cost/resolved {'n/a (0 resolved)' if per_resolved is None else f'{per_resolved:.8f}'}  "
            f"tiers {','.join(row['tiers_charged']) or '-'}"
        ))

    proof.fact("ladder", " < ".join(config.ladder.names))
    proof.fact("tasks", f"{len(tasks)} from {parsed.tasks}")
    proof.fact("per-task ceiling", f"{ceiling} {currency} (principal {parsed.principal})")
    proof.fact("judge panel", ", ".join(f"{m.provider}/{m.model}" for m in panel))
    proof.fact("wall clock", f"{elapsed:.1f}s")

    missing = {key: [t.id for t in tasks] for key in results if len(results[key]) != len(tasks)}
    proof.check("every strategy attempted every task", not missing,
                missing or f"{len(chosen)} strategies x {len(tasks)} tasks")

    ledger_calls = sum(row["calls"] for rows in results.values() for row in rows)
    answer_calls = transport.calls
    proof.check("every provider call is metered", ledger_calls == answer_calls,
                f"{answer_calls} transport calls, {ledger_calls} ledger charges")

    judge_failed = sum(row["judge_failed"] for row in summaries.values())
    proof.check("no verdict was unparseable", judge_failed == 0,
                f"{judge_failed} tasks ended with status=judge_failed")

    resolved_total = sum(row["resolved"] for row in summaries.values())
    proof.check("at least one task resolved", resolved_total > 0, f"{resolved_total} resolved")

    proof.record("summaries", summaries)
    proof.record("strategies", {key: vars(strategy) for key, strategy in available.items()})
    proof.record("tasks", [task.as_dict() for task in tasks])
    proof.record("per_task", results)
    proof.record("simulated", bool(getattr(client, "simulated", False)))
    proof.record("wall_clock_seconds", elapsed)

    print("\n  SUMMARY")
    for key, row in summaries.items():
        print(f"  {key}: {row['resolved']}/{row['tasks']} resolved, "
              f"cost/resolved = {row['cost_per_resolved_task'] or 'n/a'}")
    if mode == "offline":
        print("\n  OFFLINE: numbers are simulation, not evidence.")
    return proof


def main() -> None:
    parsed = parse_args()
    proof = run(parsed)
    OUT.mkdir(parents=True, exist_ok=True)
    sys.exit(proof.finish())


if __name__ == "__main__":
    main()