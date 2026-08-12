# S15Code — the budget-aware agent runtime

Session 15 of EAGV3. Hand a run a **ceiling** and a **principal**, and the agent
plans against them: every node declares the capability tier it needs, the
allowance is re-divided across the live frontier on every planning round, and a
controller admits, downgrades, branches or refuses each call before it is made.
The same durable event journal the graph already writes is then exported as
OpenTelemetry spans, with token usage and cost per span.

Two things are true no matter what the model does with its tokens:

- **No call is made without being metered.** The controller owns the transport;
  there is no code path to a provider that skips the ledger.
- **No run spends past its ceiling.** Enforcement is deterministic code. A budget
  asked for in a prompt leaks — models are token-elastic (TALE, 2026), so they
  sail past a tight ceiling while sincerely agreeing to it.

## One package

`S14Code` shipped two packages side by side, with the session's own work hidden
inside the previous session's namespace. This repo has **exactly one importable
package**, and nothing is nested inside a prior session's name.

```
S15Code/
├── s15code/
│   ├── main.py            FastAPI app factory
│   ├── cli.py             `s15code serve`
│   ├── routes.py          runs, facts, documents, memory search, trace
│   ├── a2a_routes.py      agent card + JSON-RPC
│   ├── gateway.py         the gateway client (never holds a credential)
│   ├── planner.py         the constrained GraphPatch proposal boundary
│   ├── runtime.py         one request through the live graph
│   ├── tools.py           the small non-browser skill surface
│   ├── core/
│   │   ├── live_graph/    executor, durable event journal, patches   (from S13)
│   │   ├── memory/        typed scoped memory, semantic chunking     (from S13)
│   │   └── a2a/           the agent-to-agent boundary                (from S13)
│   ├── ui/                catalog, validator, surface, AG-UI, HITL   (from S14)
│   ├── economics/         NEW — budget-aware planning
│   │   ├── config.py        loads the three YAML files
│   │   ├── pricing.py       per-model prices
│   │   ├── tiers.py         the capability ladder; a node declares a tier
│   │   ├── budget.py        allowance, spend, reservations, allocation
│   │   ├── policy.py        proceed / downgrade / branch / refuse
│   │   └── controller.py    the hard controller at the call seam
│   ├── telemetry/         NEW — the same journal, as OTel spans
│   │   └── spans.py         run → agent loop → plan → node → provider call
│   └── evals/             NEW — did the answer RESOLVE the task?
│       ├── config.py        the rubric, the bar and the judge panel, from YAML
│       ├── judge.py         LLM-as-judge on a generic, task-agnostic rubric
│       └── tasks.py         reads a task set; never contains one
├── config/                tiers.yaml · pricing.yaml · budgets.yaml · evals.yaml
├── proofs/                the generic proof harness
│   └── tasks/               task sets, as DATA a reviewer can replace
└── tests/
```

## The elegant reuse

The graph writes **one** durable journal. It now has three consumers, and no
parallel event system exists:

| Consumer | Reads the journal as |
|---|---|
| the executor | graph replay and crash recovery (S13) |
| `s15code.ui.agui` | AG-UI events for a browser (S14) |
| `s15code.telemetry.spans` | OpenTelemetry spans for a collector (S15) |

The controller writes each metered call into the node's own result, so the
journal carries tokens, cost, tier and the budget decision. Delete the
materialised graph and the trace still builds from the tape alone — `p4` checks
exactly that.

## Nothing is hardcoded

No tier name, model, provider, price, threshold or budget appears in Python.

| Decision | Lives in |
|---|---|
| what tiers exist, and what each expands to on the wire | `config/tiers.yaml` |
| which tier a graph role asks for | `config/tiers.yaml` → `role_tiers` |
| what a model costs, and cache-read discounts | `config/pricing.yaml` |
| default allowance, per-principal caps | `config/budgets.yaml` |
| downgrade / refuse ratios, reserve, call ceilings, token estimation | `config/budgets.yaml` |

`config/` is resolved from `S15_CONFIG_DIR` when set, otherwise from beside the
package. The unit tests build their **own** ladder with invented tier names, which
is the real check that the library never depends on the shipped ones.

## Run it

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run s15code serve            # http://127.0.0.1:8113
```

The gateway is a separate process on `http://127.0.0.1:8111` (`GLC_BASE_URL`).
S15Code holds no provider credential; copy `.env.example` to `.env` and set paths,
never keys.

A budgeted run over HTTP:

```bash
curl -s localhost:8113/v1/agent/runs -H 'content-type: application/json' -d '{
  "tenant_id": "acme", "project_id": "research", "user_id": "rohan",
  "prompt": "<any task>", "budget": 0.02
}' | jq '.budget'
```

The response carries a `budget` ledger (total, spent, remaining, pressure, every
charge, every refusal), the `allocations` the planner made each round, and the
`tier` each node declared. `GET /v1/agent/runs/{id}/trace` returns the same run as
a span tree.

Omit `budget` and the run behaves exactly as it did before economics existed —
the layer is additive.

## Proofs

One harness, one code path, six proofs. Each takes the **task (or task set, or
pair set), budget and principal as arguments**, asserts real invariants, exits
non-zero on failure, and writes JSON to `proofs/out/`.

```bash
uv run python proofs/p1_cost_per_task.py    --tasks proofs/tasks/mixed.jsonl
uv run python proofs/p2_budget_holds.py     --task "<any task>" --budget 0.02
uv run python proofs/p3_denial_of_wallet.py --task "<any task>" --budget 0.002
uv run python proofs/p4_trace_export.py     --task "<any task>" --budget 0.02
uv run python proofs/p6_cache_savings.py    --pairs proofs/pairs/paraphrases.jsonl
uv run python proofs/p7_cross_model_ladder.py --task "<any task>"
```

| Proof | Proves |
|---|---|
| `p1_cost_per_task` | The same task set through always-frontier, always-cheapest-with-retries and the budget-aware cascade, on one ledger. Reports spend, calls, cost per call and **cost per resolved task** per strategy, with "resolved" decided by a generic rubric judge (`s15code.evals`) and never by a per-task answer key. Whether the cheapest rung shows the signature failure mode — lower cost per call, higher cost per resolved task — is reported as a finding, not asserted. |
| `p2_budget_holds` | A run given a ceiling stays under it at every ceiling; a tight allowance downgrades the tier a node asked for; an unaffordable ceiling refuses instead of overspending; provider calls and ledger entries agree exactly. |
| `p3_denial_of_wallet` | An adversarial planner that earns one more node from every outcome, forever, cannot spend past the ceiling. Refusals are visible graph failures. Reports the bill the same loop would have run up uncontrolled. |
| `p4_trace_export` | The journal exports as `run → agent loop → plan → node → provider call`, with `gen_ai.usage.*` and cost on every provider-call span, summing exactly to the ledger. Content capture off. Works with no collector. |
| `p6_cache_savings` | The gateway's semantic cache, against the **real** embedder. A 768-dim nomic vector is confirmed to be neither a stub nor a constant; the similarity the gateway acts on is checked against a cosine computed independently; a hit is billed $0 and its saving is read off the cold call it replaced. Then the threshold is **swept** over a labelled pair set (`proofs/pairs/`), reporting true- and false-positive rates per threshold and per negative family. Whether a collision-free threshold exists is a finding, not an assertion — and on the shipped set it does not. |
| `p7_cross_model_ladder` | Every rung is a different model on a different provider; budget pressure walks the whole ladder down, one model at a time; projected cost is monotone; the measured top-to-bottom spread is reported as a multiple. |

**Two modes, one code path.** If the gateway at `--base-url` answers, the proofs
make real calls and meter real money. Otherwise (or with `--offline`) a
deterministic transport stands in — the policy, ladder, budget, journal and span
export are all the real implementation, only the network is replaced. That is what
lets CI run the same proof with no key and no collector.

The ceilings `p2` uses to force a downgrade and a refusal are **derived from the
configured ladder**, not written down, so editing `config/tiers.yaml` changes the
numbers rather than breaking the proof.

Useful flags: `--offline`, `--respond-as ui`, `--principal tenant/project/user`,
`--otel-endpoint http://127.0.0.1:4318/v1/traces`, `--config-dir`, `--label`,
`--live-embeddings`.

## Observability

`s15code.telemetry.export_run` turns a journal into spans through the real OTel
SDK. Attributes follow the GenAI semantic conventions —
`gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`. Those conventions are
pre-stable and moved to their own repository in June 2026 with no tagged release,
so **cost has no blessed attribute yet**: it is emitted as `s15.cost` with
`s15.currency`, clearly marked as a vendor extension rather than pretending to be
standard.

Set `S15_OTEL_EXPORTER_ENDPOINT` (or `--otel-endpoint`) to send the spans to
Jaeger or any OTLP receiver; Jaeger ingests OTLP natively. Leave it unset and the
span tree is still built and assertable in memory while nothing goes over the
wire, so tests need no collector.

**Content capture is off by default.** Prompts and completions are PII. They are
attached only when a caller passes `capture_content=True` or sets
`S15_OTEL_CAPTURE_CONTENT=1`.

## What the meter covers, honestly

The ledger covers **gateway model calls** — the paid ones. Semantic-memory
embeddings run locally against Ollama and never touch the gateway, so they cost
nothing per token and are not in the ledger.

Admission prices the **worst case**: output is bounded by the tier's `max_tokens`,
which the provider honours, and input by an over-estimate of the prompt actually
being sent (`chars_per_token` and `input_estimate_safety` in `budgets.yaml`). That
makes the projection a real upper bound rather than an average. If a provider
ignored `max_tokens` the call already in flight could overshoot — so the ledger is
also an absolute stop, and `max_calls_per_run` / `max_calls_per_node` bound the
loop even when every price estimate is wrong. A test drives exactly that case.

## Carried forward vs new

**Carried forward** (imports renamed to `s15code.*`, behaviour unchanged): the
live graph and its journal, scoped memory and semantic chunking, the A2A boundary
and gRPC binding, the A2UI catalog/validator/surface/AG-UI/HITL layer, the
gateway boundary, the deterministic and LLM planners, the non-browser skills.

**New in this session**: `s15code/economics/` (six modules), `s15code/telemetry/`,
`config/` (three files), the `budget`/`principal` arguments on a run, the
`/trace` route, and a rewritten `proofs/` harness.

**Deliberately dropped**: S14's `showcase.py` and its `/dashboard` route hardcoded
one use case (a five-paper research corpus, with its title in the code). The
`/v1/harness/surface` route depended on one specific S14 proof artefact. Both
would have violated the no-hardcoding rule this session is built around.

The generated protobuf modules under `s15code/core/a2a/` keep their original
filenames. They are reproduced verbatim because the serialized descriptor is
keyed on the `.proto` file name, and hand-editing generated gencode is worse than
a stale name.

---

# Evidence — a routing policy for code review, and what it really cost

Session 15 assignment. Everything below was run against real providers on
2026-08-11, not simulated. The short version:

- A cheap-to-strong cascade **resolved every task** (16/16) and was **29% cheaper
  per resolved task** than always using the expensive model.
- But it **lost to always-using-the-cheapest** by 4.7%, by a margin so thin
  (1.8 percentage points) that the honest answer is "these two are tied".
- The expensive model **failed a task both cheaper models solved**.
- An attack on the budget was refused 112 times out of 120, and the refusal is
  visible in the trace with the reason attached.

Full working: [`docs/PART2_PART3_EVIDENCE.md`](docs/PART2_PART3_EVIDENCE.md).
Part 1 (reproducing the shipped floor): [`docs/PART1_EVIDENCE.md`](docs/PART1_EVIDENCE.md).

## The workload

**Code review.** Each task shows one function and asks for three things: name the
defect, describe a concrete scenario where it goes wrong, and give corrected code.

`proofs/tasks/code_review.jsonl` — 16 tasks, 4 trivial, 5 moderate, 7 hard.
Generated by `proofs/tasks/code_review.py` so the code snippets inside the JSON are
escaped by the machine rather than by hand.

Why the tasks demand long answers: an earlier run cost **5x** another *from the same
model* purely because it wrote more. If answers are short, every model costs about
the same and the whole comparison shows nothing.

One task, `t15_no_defect`, contains **no bug at all**. A model that invents one fails
it. That is the cheap model's characteristic failure — confident fabrication — and it
was planted on purpose so the policy had somewhere to be visibly wrong.

## The ladder

Three models, cheapest first. A "rung" is just a named model the policy can pick.

```
rung      provider  model                  price in/out   cost per call (worst case)
economy   cerebras  zai-glm-4.7            0.50 / 0.50    $0.00110
standard  groq      openai/gpt-oss-120b    0.15 / 0.75    $0.00129
frontier  gemini    gemini-3.1-flash-lite  0.25 / 1.50    $0.00255
```

Three different providers, and cost rises at every step. Config:
`config_code_review/`, selected with `--config-dir` so the shipped config is
untouched.

**Every rung gets the same token limit (1600).** The shipped ladder gives the
expensive rung 8x more room than the cheap one. Measured here, a hard review needs
up to 844 tokens — so under the shipped 512-token cheap rung the answer gets cut off
mid-sentence and is marked wrong *for running out of room*, not for being a worse
model. Those two failures look identical in the final number. Equal limits mean the
only thing that differs between rungs is the model itself.

The price of that fairness: the gap between cheapest and dearest is **2.32x**, not
the shipped 26x. That makes the bar much harder to clear, which is the point.

## The numbers

Three strategies over the same 16 tasks, one ledger, two independent LLM judges
deciding whether each answer actually resolved its task.

```
                    total spend  calls  cost per call  resolved  COST PER RESOLVED
A always-frontier    $0.0092970    16     $0.00058106   15/16       $0.00061980
B always-cheapest    $0.0058802    20     $0.00029401   14/16       $0.00042001
C budget-aware       $0.0070506    18     $0.00039170   16/16       $0.00044066
```

**Cost per resolved task is the number that matters.** Cost per call always flatters
the cheap model, because it ignores that a wrong answer has to be paid for twice.

Against the always-frontier baseline the cascade wins clearly: **28.9% cheaper per
resolved task, and it resolves more** (16/16 against 15/16).

## Break-even, and where we sit

Break-even is the resolution rate the cheap model must hit to be worth using. Below
it, retrying cheap costs more than just paying for the good model.

```
vs always-frontier   break-even 73.0%   cheap model got 87.5%   ->  14.5 points clear
vs the cascade       break-even 85.7%   cheap model got 87.5%   ->   1.8 points clear
```

The second line is the honest result. **The cascade lost.** Always-cheapest came out
4.7% cheaper per resolved task — but by 1.8 percentage points, on 16 tasks. Two tasks
either way would flip it.

So the conclusion is not "always-cheapest wins". It is that **at this ladder's 2.32x
spread the two strategies are indistinguishable on cost**, and what the cascade
actually buys is the resolution rate: 100% against 87.5%. If a missed review costs
more than $0.0002, the cascade is worth it. That is a business question, not a
measurement one, and the measurement says so rather than pretending otherwise.

## One case the policy got wrong

**`t10_check_then_act` — the most expensive model failed what both cheaper ones
solved.**

A race condition: two threads check "is this slot free?", both see yes, both write.
`gemini-3.1-flash-lite` (the frontier rung) did not resolve it. `zai-glm-4.7` and
`openai/gpt-oss-120b` both did, first attempt.

```
A spent $0.00058106 on the frontier rung and got nothing usable
C spent $0.00039790 on the cheapest rung and resolved it
```

Paying **1.46x for a worse answer**. On this ladder price does not predict quality —
which is why the policy opens on `standard` rather than `frontier`.

## The planted trap fired

```
t15_no_defect   always-cheapest -> economy, economy, economy -> NEVER RESOLVED
                budget-aware    -> economy, then standard    -> resolved
```

The function with no bug. The cheap model invented a defect three times running and
never backed down. Retrying the same model cannot fix confident fabrication;
**changing model can** — and that is the one thing a cascade does that a retry loop
does not. It is one of only two tasks the cheap-only strategy lost.

## A Jaeger trace with costs

Every run exports as a span tree. One provider call, with cost and tokens attached:

```
run
 └─ agent_loop
     └─ plan
         └─ node
             └─ provider_call    gemini-2.5-flash
                                 gen_ai.usage.input_tokens   227
                                 gen_ai.usage.output_tokens  503
                                 s15.cost                    0.0013256
```

The trace and the ledger are built from the same journal by different readers, and
`p4` checks they agree: **span costs summed to the ledger with a difference of
exactly 0.000e+00**. No prompt or answer text reaches the backend — verified against
the live backend, not just asserted in config.

## The attack

`proofs/p8_adversarial_code_review.py` — written against *this* policy, aimed at the
two controls it deliberately loosened (`downgrade_at` 0.50→0.65,
`max_calls_per_node` 6→4). All 11 checks pass.

The adversary is a planner that earns a new task from every outcome, forever, always
asking for the most expensive rung. Run twice — once with the ceiling raised so high
it never bites, once with the real policy:

```
BEFORE  no effective ceiling    12 calls   spent $0.01490625     0 refusals
AFTER   the real policy          8 calls   spent $0.00917880   112 refusals
                                                               120 rounds attempted
```

It never stopped asking. **120 attempts, 112 refused, 8 allowed.** Spend stopped at
91.8% of the ceiling because `refuse_at: 0.90` said so — not because the attacker
gave up. Extrapolated from this run's own measured cost per call, the uncontrolled
version bills **~$12.42 over 10,000 rounds**.

Two more targeted attacks, both held:

```
unaffordable ceiling   asked for the dearest rung with less money than the CHEAPEST
                       rung costs  ->  0 calls, $0 spent, 1 refusal
one greedy node        a single task calling the model 20 times
                       ->  4 allowed, then refused (max_calls_per_node = 4, exactly)
```

The first is the subtle one: it **refused rather than downgrading** to something that
still would not fit, and contacted no provider at all.

## The refusal is visible in telemetry

Fetched back out of Jaeger by trace id, the refused nodes carry the reason:

```
otel.status_description
  BudgetRefused: budget refused a frontier call for loop_9:
  spend pressure 0.918 >= refuse_at 0.9
```

The control, the threshold, and the measured pressure that tripped it. A trace saying
only "this failed" cannot audit a budget.

And **refused nodes emit no `provider_call` span at all** — 8 provider-call spans for
8 allowed calls, against 120 nodes. The absence is the evidence that nothing was sent.

## Honest limitations

**1. Judging cost more than the work, and the panel degraded.**

```
judge   37 calls   $0.01711875   35 unusable samples   226 transport retries
work                $0.02223
```

The judge was heavily rate-limited, so many verdicts fell back to a single grader
instead of a two-member panel. Verdicts still parsed, but *disagreement between
judges* stopped being measurable. With the headline result resting on 1.8 percentage
points, this is a real threat to the conclusion, not a footnote. A re-run with a
rate-limit-tolerant panel could flip it.

**2. Nothing was actually billed.** All providers are on free tiers. Every dollar
figure is computed from `pricing.yaml` rates against real reported token counts. The
control logic is real; the invoice is not.

**3. A gateway defect makes one model's spend invisible.** `gemini-2.5-flash` was the
frontier rung for one run and had to be removed. `glc_v4/glc/providers.py` guards its
thinking config with `if reasoning and reasoning != "off"`, so asking for
`reasoning: "off"` sends nothing — and that model thinks by default, so "off" leaves
thinking **on**. Its thought tokens then eat the answer budget (8 of 12 calls returned
~61 visible tokens, truncated mid-review) and are **never metered**, because the
parser reads `candidatesTokenCount` and ignores `thoughtsTokenCount`. Google bills
them; this ledger never sees them. In a system whose stated invariant is that no call
escapes the ledger, that is unmetered spend.

**4. Downgrading is a weak control on a narrow ladder.** At 2.32x, dropping a rung
saves 57%; on the shipped 26x ladder it saves 96%. This policy therefore leans on the
absolute ceiling and the call counters rather than on graceful degradation.

## Reproducing this from a fresh checkout

```bash
git clone https://github.com/theschoolofai/glc_v4.git
git clone https://github.com/theschoolofai/S15Code.git

# Jaeger. No Docker needed — this is the project's own fallback.
glc_v4/scripts/jaeger_local.sh --start

# Gateway. Keys go in glc_v4/.env and are never committed.
cd glc_v4 && uv sync && uv run glc serve            # port 8111

# Runtime, in a second terminal.
cd S15Code && uv sync && uv run s15code serve       # port 8113

cd S15Code
B=http://127.0.0.1:8111
OTLP=http://127.0.0.1:4318/v1/traces

python proofs/tasks/code_review.py                  # regenerate the task set

# Part 2 — the measurement. Takes about 2.4 hours; the judge is rate-limited.
uv run python proofs/p1_cost_per_task.py \
    --tasks proofs/tasks/code_review.jsonl \
    --config-dir config_code_review \
    --principal course/s15/part2 --label code_review_v2 --base-url $B

# Part 3 — the attack.
uv run python proofs/p8_adversarial_code_review.py \
    --task "adversarial budget test" \
    --config-dir config_code_review --budget 0.01 --uncontrolled-rounds 12 \
    --principal course/s15/part3 --label code_review \
    --base-url $B --otel-endpoint $OTLP
```

**`--base-url` is required.** The proofs default to port 8112, and when no gateway
answers there they silently swap in a fake transport and pass anyway — with invented
numbers and no trace. Two other commands in the session handout are also silently
wrong; both are documented in `docs/PART1_EVIDENCE.md`.

The environment variable for tracing is `OTEL_EXPORTER_OTLP_ENDPOINT`, not
`GLC_OTEL_EXPORTER_ENDPOINT`. Nothing reads the latter, and the exporter is a no-op
when unconfigured, so the gateway looks healthy and exports nothing.

**Running the test suite.** On a clean checkout it is simply green:

```bash
uv sync && uv run pytest -q          # 277 passed
```

Verified by cloning this branch into an empty directory and running it there.

It only goes red once you add a `.env` configuring a real collector for the proofs.
`s15code/main.py` calls `load_dotenv`, so the tests read your local `.env`, and
`test_the_trace_route_serves_the_span_tree` asserts `exported_over_the_wire is False`
on the grounds that no collector is needed. True on a clean checkout; false the
moment you point the runtime at Jaeger.

The fix is to set the variable **empty** rather than to unset it:

```bash
S15_OTEL_EXPORTER_ENDPOINT= uv run pytest -q          # 277 passed
env -u S15_OTEL_EXPORTER_ENDPOINT uv run pytest -q    # still fails
```

`load_dotenv` does not override a variable that is already set, so an empty value
wins, while unsetting it just lets the `.env` value back in. The suite is not
hermetic with respect to the developer's environment.

Results land in `proofs/out/`, which is gitignored — the numbers above are the record.
