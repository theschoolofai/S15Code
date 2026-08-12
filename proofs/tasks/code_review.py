#!/usr/bin/env python3
"""Generate proofs/tasks/code_review.jsonl.

The task set is DATA, but writing it by hand means hand-escaping newlines inside
JSON strings for sixteen code snippets, which is how subtly-corrupted fixtures get
made. So the snippets live here as ordinary triple-quoted Python and json.dumps
does the escaping.

Design rules, each of which exists for a measured reason:

* **Every answer must be long.** A captured run on 2026-08-11 cost 5x another from
  the SAME model purely because it wrote more (1850 output tokens vs 353). A task
  set whose answers are short collapses the ladder's cost spread to nothing and
  makes every routing strategy look identical. So the instruction demands three
  things — name the defect, give a concrete failure scenario, give corrected code —
  which no model can satisfy in two lines.

* **One defect per snippet.** The `expectation` is scored at double weight against
  the answer. If a snippet has two defensible bugs, a correct answer that names the
  other one gets marked wrong, and the measurement is noise.

* **The instruction is identical everywhere.** The only variable across tasks is
  the code, so difficulty is a property of the defect rather than of the prompt.

* **One snippet is clean.** t15 has no defect. A model that invents one fails it.
  This is the cheap rung's characteristic failure — confident fabrication — and it
  is deliberately included so the policy has somewhere to be wrong.
"""
import json
from pathlib import Path

INSTRUCTION = (
    "You are reviewing a single function during code review.\n\n"
    "Identify the defect. Name it precisely, describe one concrete scenario in which "
    "it produces wrong behaviour (state the inputs and the resulting output), and give "
    "the corrected code. If the function is correct as written, say so explicitly and "
    "justify why the constructs that look suspicious are in fact safe.\n\n"
    "Code under review:\n"
)

TASKS = [
    # ---------------------------------------------------------------- trivial
    ("t01_offbyone", "trivial", '''
def largest(items):
    """Return the largest number in items."""
    best = items[0]
    for i in range(1, len(items) - 1):
        if items[i] > best:
            best = items[i]
    return best
''', "Identifies that the loop bound `len(items) - 1` skips the final element, so the "
     "last item is never compared. A correct answer gives an input whose maximum is "
     "last (e.g. [1, 2, 9]) and states the wrong result (2 instead of 9), and fixes the "
     "range to cover the whole list."),

    ("t02_mutable_default", "trivial", '''
def collect(value, bucket=[]):
    """Append value to bucket and return it."""
    bucket.append(value)
    return bucket
''', "Identifies the mutable default argument: the list is created once at function "
     "definition, so state leaks between calls that do not pass `bucket`. A correct "
     "answer shows two successive calls accumulating (collect(1) -> [1], collect(2) -> "
     "[1, 2]) and fixes it with a None sentinel."),

    ("t03_int_division", "trivial", '''
def average_score(scores):
    """Return the mean of a list of integer scores."""
    total = 0
    for s in scores:
        total += s
    return total // len(scores)
''', "Identifies that `//` is floor division, so the mean is truncated to an integer. A "
     "correct answer gives an input where this matters (e.g. [1, 2] returning 1 rather "
     "than 1.5) and changes the operator to `/`."),

    ("t04_identity_compare", "trivial", '''
def is_admin(user_role):
    """True when the supplied role string names the admin role."""
    if user_role is "admin":
        return True
    return False
''', "Identifies that `is` compares object identity, not string value, so the check "
     "depends on interning and fails for a runtime-constructed string. A correct answer "
     "notes it may appear to work for literals, gives a failing case (a role read from "
     "input or built by concatenation), and replaces `is` with `==`."),

    # --------------------------------------------------------------- moderate
    ("t05_file_leak", "moderate", '''
def load_all(paths):
    """Read every file and return the list of contents."""
    out = []
    for p in paths:
        f = open(p)
        out.append(f.read())
    return out
''', "Identifies that each file handle is never closed, leaking descriptors across the "
     "loop until the process hits its limit on a large `paths`. A correct answer "
     "rewrites the loop using a `with` block."),

    ("t06_swallowed_error", "moderate", '''
def parse_config(text):
    """Return the parsed config, or an empty dict if it is unusable."""
    try:
        return json.loads(text)
    except Exception:
        pass
    return {}
''', "Identifies that a bare `except Exception: pass` hides every failure, including "
     "programming errors like a missing `json` import or a NameError, and silently "
     "returns an empty config that the caller cannot distinguish from a legitimately "
     "empty one. A correct answer narrows the except to the parse error and either logs "
     "or re-raises the rest."),

    ("t07_mutate_while_iterating", "moderate", '''
def drop_expired(sessions):
    """Remove every expired session from the list, in place."""
    for s in sessions:
        if s.expired:
            sessions.remove(s)
    return sessions
''', "Identifies that removing from a list while iterating it makes the iterator skip "
     "elements, because the index advances while the list shifts left. A correct answer "
     "gives a case with two adjacent expired sessions where the second survives, and "
     "fixes it by building a new list or iterating over a copy."),

    ("t08_sql_injection", "moderate", '''
def find_user(conn, email):
    """Look up one user row by email address."""
    query = f"SELECT id, name FROM users WHERE email = '{email}'"
    return conn.execute(query).fetchone()
''', "Identifies SQL injection via string interpolation of untrusted input into the "
     "query. A correct answer gives a concrete malicious `email` value that changes the "
     "statement's meaning, and rewrites it using a parameterised query with a "
     "placeholder rather than escaping the input by hand."),

    ("t09_retry_all_errors", "moderate", '''
def fetch_with_retry(url, attempts=5):
    """Fetch a URL, retrying transient failures."""
    for _ in range(attempts):
        try:
            return http_get(url)
        except Exception:
            continue
    raise RuntimeError("all attempts failed")
''', "Identifies that the retry loop catches every exception, so permanent failures "
     "(404, malformed URL, authentication rejection) are retried as if transient, and "
     "that there is no delay between attempts so retries hammer the server immediately. "
     "A correct answer distinguishes retryable from non-retryable errors and adds "
     "backoff. Reporting only one of the two issues is partial."),

    # ------------------------------------------------------------------- hard
    ("t10_check_then_act", "hard", '''
def reserve_slot(bookings, slot_id, user):
    """Reserve a slot for a user if nobody holds it yet."""
    if slot_id not in bookings:
        bookings[slot_id] = user
        return True
    return False
''', "Identifies the check-then-act race: between the membership test and the "
     "assignment, another thread can insert the same key, so two callers both observe "
     "the slot as free and the second silently overwrites the first, with both "
     "receiving True. A correct answer notes the operation is not atomic despite each "
     "dict operation being individually safe, and fixes it with a lock or an atomic "
     "primitive such as setdefault."),

    ("t11_float_money", "hard", '''
def total_cents(prices):
    """Sum a list of prices given in dollars, returning whole cents."""
    total = 0.0
    for p in prices:
        total += p
    return int(total * 100)
''', "Identifies that binary floating point cannot represent decimal fractions exactly, "
     "so accumulated error plus truncation by `int()` loses a cent. A correct answer "
     "gives a concrete case (such as 0.1 + 0.2 summing to 0.30000000000000004, or a "
     "total landing just below a whole cent and truncating downward), and fixes it with "
     "Decimal or by working in integer cents throughout. Merely suggesting round() "
     "without addressing the representation is partial."),

    ("t12_unbounded_cache", "hard", '''
_cache = {}

def render(template_name, context):
    """Render a template, caching the compiled result."""
    key = (template_name, tuple(sorted(context.items())))
    if key not in _cache:
        _cache[key] = compile_template(template_name).render(context)
    return _cache[key]
''', "Identifies that the cache key includes the per-request context, so the dictionary "
     "gains a new entry for every distinct set of values and grows without bound — a "
     "memory leak, not a cache. A correct answer explains that the compiled template is "
     "what is reusable while the rendered output generally is not, and fixes it by "
     "keying only on the template name or by bounding the cache."),

    ("t13_blocking_async", "hard", '''
async def poll_until_ready(job_id):
    """Wait for a job to finish, checking once a second."""
    while True:
        status = await get_status(job_id)
        if status == "done":
            return status
        time.sleep(1)
''', "Identifies that `time.sleep` blocks the event loop thread rather than yielding, so "
     "every other coroutine in the process is frozen for the duration — the whole "
     "server stalls, not just this task. A correct answer replaces it with `await "
     "asyncio.sleep(1)` and explains why awaiting is what releases the loop."),

    ("t14_naive_datetime", "hard", '''
def is_stale(record, max_age_hours=24):
    """True when the record was created more than max_age_hours ago."""
    created = datetime.fromisoformat(record["created_at"])
    age = datetime.now() - created
    return age.total_seconds() > max_age_hours * 3600
''', "Identifies that `datetime.now()` returns local time while the stored timestamp is "
     "typically UTC, so the comparison is off by the UTC offset — and that mixing a "
     "naive and an aware datetime raises TypeError outright. A correct answer names the "
     "concrete consequence (records judged stale or fresh several hours early or late, "
     "and the boundary shifting at daylight-saving transitions) and fixes it by "
     "comparing in a single explicit timezone."),

    ("t15_no_defect", "hard", '''
def chunked(items, size):
    """Split items into consecutive lists of at most `size` elements."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [items[i:i + size] for i in range(0, len(items), size)]
''', "States explicitly that the function is CORRECT as written and that no fix is "
     "needed. A correct answer justifies why the constructs that look risky are safe: "
     "the slice `items[i:i + size]` cannot go out of range because Python slicing "
     "clamps, the final chunk is legitimately shorter, an empty input yields an empty "
     "list, and the guard rejects a non-positive size. An answer that invents a defect "
     "or proposes a rewrite fails this task however plausible it sounds."),

    ("t16_binary_search", "hard", '''
def first_at_least(sorted_values, target):
    """Index of the first element >= target, or len() if none is."""
    low, high = 0, len(sorted_values)
    while low < high:
        mid = (low + high) // 2
        if sorted_values[mid] < target:
            low = mid
        else:
            high = mid
    return low
''', "Identifies that `low = mid` fails to advance the lower bound, so when `high` is "
     "`low + 1` the interval never shrinks and the loop spins forever. A correct answer "
     "gives a concrete input that hangs (such as [1, 2] with target 2, where low=0, "
     "high=2, mid=1 leaves low unchanged... or any case reaching a two-element window "
     "with the answer above mid) and fixes it to `low = mid + 1`."),
]


def main() -> None:
    out = Path(__file__).resolve().parent / "code_review.jsonl"
    lines = [
        "# Code-review task set for p1. THIS FILE IS DATA — regenerate with code_review.py.",
        "#",
        "# 16 tasks: 4 trivial, 5 moderate, 7 hard. Every task asks for a named defect, a",
        "# concrete failure scenario and corrected code, so answers are long enough for the",
        "# ladder's cost spread to be visible. t15 has NO defect and fails any answer that",
        "# invents one.",
    ]
    for task_id, difficulty, code, expectation in TASKS:
        record = {
            "id": task_id,
            "difficulty": difficulty,
            "task": INSTRUCTION + "```python" + code.rstrip() + "\n```",
            "expectation": expectation,
        }
        lines.append(json.dumps(record))
    out.write_text("\n".join(lines) + "\n")

    longest = max(len(json.loads(line)["task"]) for line in lines if line.startswith("{"))
    print(f"wrote {out}")
    print(f"  {len(TASKS)} tasks, longest task text {longest} chars (judge cap is 4000)")


if __name__ == "__main__":
    main()
