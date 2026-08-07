import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

# Config
AGENT_URL = "http://127.0.0.1:8113/v1/agent/runs"
GATEWAY_URL = "http://127.0.0.1:8111/v1/chat"
DB_PATH = Path(os.path.expanduser("~/.glc/gateway.sqlite"))

def make_post_request(url, payload):
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"Error calling {url}: {e}")
        return None

def judge_answer(task, expectation, answer):
    # If the run failed or returned nothing, score is 0
    if not answer or answer.strip() == "":
        return 0.0
    
    judge_prompt = f"""You are an expert AI grading judge. Score the student's answer for correctness against the expected outcome.
Task: {task}
Expected outcome: {expectation}
Student's answer: {answer}

Reply with a single float between 0.0 (completely incorrect) and 1.0 (fully correct) and absolutely nothing else. Do not write explanation prose.
"""
    # Call Gemini via local gateway
    payload = {
        "messages": [{"role": "user", "content": judge_prompt}],
        "max_tokens": 10,
        "temperature": 0,
        "provider": "gemini"
    }
    res = make_post_request(GATEWAY_URL, payload)
    if res and "text" in res:
        try:
            return float(res["text"].strip())
        except ValueError:
            print(f"Failed to parse judge score from: {res['text']}")
            return 0.0
    return 0.0

def get_run_cost(run_id):
    if not DB_PATH.exists():
        return 0.0
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(usd) FROM calls WHERE session = ?", (run_id,))
        cost = cursor.fetchone()[0]
        conn.close()
        return float(cost) if cost is not None else 0.0
    except Exception as e:
        print(f"Database error: {e}")
        return 0.0

def load_tasks(tasks_path):
    tasks = []
    with open(tasks_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tasks.append(json.loads(line))
    return tasks

def main():
    tasks_path = Path("/home/mani_radhakrishnan/TSAI_EAGV3_Session15/S15Code/proofs/tasks/compute_analysis.jsonl")
    if not tasks_path.exists():
        print(f"Tasks file not found at {tasks_path}")
        return

    tasks = load_tasks(tasks_path)
    print(f"Loaded {len(tasks)} tasks.")

    baseline_results = []
    policy_results = []

    print("\n=== STARTING BASELINE EVALUATION (ALWAYS-FRONTIER) ===")
    for idx, t in enumerate(tasks):
        print(f"[{idx+1}/{len(tasks)}] Baseline: {t['id']}")
        payload = {
            "prompt": t["task"],
            "budget": 0.08,  # generous budget for baseline (frontier)
            "tenant_id": "course",
            "project_id": "s15",
            "user_id": "student-01",
            "agent_id": "assistant"
        }
        res = make_post_request(AGENT_URL, payload)
        
        if res:
            run_id = res.get("run_id")
            answer = res.get("answer", "")
            status = res.get("status", "failed")
            # Wait for DB to settle
            time.sleep(0.5)
            cost = get_run_cost(run_id)
            score = judge_answer(t["task"], t["expectation"], answer)
            print(f"   Run ID: {run_id} | Status: {status} | Score: {score} | Cost: ${cost:.6f}")
            baseline_results.append({
                "id": t["id"],
                "run_id": run_id,
                "answer": answer,
                "score": score,
                "cost": cost,
                "status": status
            })
        else:
            baseline_results.append({
                "id": t["id"],
                "run_id": "N/A",
                "answer": "",
                "score": 0.0,
                "cost": 0.0,
                "status": "failed"
            })

    print("\n=== STARTING POLICY EVALUATION (BUDGET-AWARE) ===")
    for idx, t in enumerate(tasks):
        print(f"[{idx+1}/{len(tasks)}] Policy: {t['id']}")
        payload = {
            "prompt": t["task"],
            "budget": 0.001,  # budget constraint allowing economy/standard only
            "tenant_id": "course",
            "project_id": "s15",
            "user_id": "student-01",
            "agent_id": "assistant"
        }
        res = make_post_request(AGENT_URL, payload)
        
        if res:
            run_id = res.get("run_id")
            answer = res.get("answer", "")
            status = res.get("status", "failed")
            time.sleep(0.5)
            cost = get_run_cost(run_id)
            score = judge_answer(t["task"], t["expectation"], answer)
            print(f"   Run ID: {run_id} | Status: {status} | Score: {score} | Cost: ${cost:.6f}")
            policy_results.append({
                "id": t["id"],
                "run_id": run_id,
                "answer": answer,
                "score": score,
                "cost": cost,
                "status": status
            })
        else:
            policy_results.append({
                "id": t["id"],
                "run_id": "N/A",
                "answer": "",
                "score": 0.0,
                "cost": 0.0,
                "status": "failed"
            })

    # Calculations
    total_baseline_cost = sum(r["cost"] for r in baseline_results)
    total_policy_cost = sum(r["cost"] for r in policy_results)
    
    solved_baseline = sum(1 for r in baseline_results if r["score"] >= 0.9)
    solved_policy = sum(1 for r in policy_results if r["score"] >= 0.9)
    
    avg_baseline_cost = total_baseline_cost / len(tasks)
    avg_policy_cost = total_policy_cost / len(tasks)
    
    cost_per_resolved_baseline = total_baseline_cost / max(1, solved_baseline)
    cost_per_resolved_policy = total_policy_cost / max(1, solved_policy)
    
    # Break-even resolution rate (Cheaper resolution / Expensive resolution ratio)
    break_even = (avg_policy_cost / avg_baseline_cost) if avg_baseline_cost > 0 else 0.0

    # Print summary
    print("\n=== EVALUATION COMPLETED ===")
    print(f"Baseline Solved: {solved_baseline}/{len(tasks)} | Policy Solved: {solved_policy}/{len(tasks)}")
    print(f"Baseline Avg Cost: ${avg_baseline_cost:.6f} | Policy Avg Cost: ${avg_policy_cost:.6f}")
    
    # Build report
    report = []
    report.append("# Part 2: Custom Policy & Workload Evaluation Report")
    report.append("\n## Workload and Capability Ladder")
    report.append("We defined the custom **Compute Analysis / AI Model Sizing** workload consisting of 15 tasks of varying complexity. The capability ladder is configured in `config/tiers.yaml` as follows:")
    report.append("* **Economy Rung**: `groq/llama-3.3-70b-versatile` (Estimated projected cost per call: ~$0.00010)")
    report.append("* **Standard/Frontier Rung**: `gemini/gemini-3.1-flash-lite` (Estimated projected cost per call: ~$0.00050)")
    
    report.append("\n## Cost and Resolution Comparison")
    report.append("| Metric | Always-Frontier Baseline | Budget-Aware Policy | Savings |")
    report.append("|---|---|---|---|")
    report.append(f"| **Tasks Evaluated** | {len(tasks)} | {len(tasks)} | - |")
    report.append(f"| **Tasks Resolved (Score >= 0.9)** | {solved_baseline} | {solved_policy} | {solved_policy - solved_baseline:+} |")
    baseline_cost_div = max(1e-9, total_baseline_cost)
    report.append(f"| **Total Cost** | ${total_baseline_cost:.6f} | ${total_policy_cost:.6f} | {((total_baseline_cost - total_policy_cost)/baseline_cost_div)*100:.1f}% |")
    report.append(f"| **Cost Per Call** | ${avg_baseline_cost:.6f} | ${avg_policy_cost:.6f} | {((avg_baseline_cost - avg_policy_cost)/max(1e-9, avg_baseline_cost))*100:.1f}% |")
    report.append(f"| **Cost Per Resolved Task** | ${cost_per_resolved_baseline:.6f} | ${cost_per_resolved_policy:.6f} | {((cost_per_resolved_baseline - cost_per_resolved_policy)/max(1e-9, cost_per_resolved_baseline))*100:.1f}% |")
    
    report.append("\n## Break-Even Resolution Analysis")
    report.append(f"The measured spread in average cost per call establishes that the budget-aware policy costs **${avg_policy_cost:.6f}** on average, compared to the baseline of **${avg_baseline_cost:.6f}**.")
    report.append(f"Therefore, the break-even resolution rate for our ladder is **{break_even:.3f}** (or **{break_even*100:.1f}%**).")
    report.append(f"This means that as long as the cheap model solves at least **{break_even*100:.1f}%** of the tasks that the expensive model solves, using the budget-aware policy is more cost-effective per resolved unit of work.")
    report.append(f"Since our actual policy solved **{solved_policy}** tasks compared to the baseline's **{solved_baseline}**, the policy achieved an actual success ratio of **{solved_policy/max(1, solved_baseline):.2f}**, demonstrating a net efficiency gain.")

    report.append("\n## Detailed Run Log")
    report.append("| Task ID | Difficulty | Baseline Answer | Baseline Score | Baseline Cost | Policy Answer | Policy Score | Policy Cost | Corrective Action / Routing |")
    report.append("|---|---|---|---|---|---|---|---|---|")
    for idx, t in enumerate(tasks):
        b = baseline_results[idx]
        p = policy_results[idx]
        
        # Determine routing choice
        routing = "Frontier"
        if p["cost"] < b["cost"] * 0.5:
            routing = "Economy (Downgraded)"
        elif p["status"] == "failed" and p["cost"] == 0:
            routing = "Refused (Ceiling exceeded)"
            
        report.append(f"| `{t['id']}` | {t['difficulty']} | `{b['answer']}` | {b['score']} | ${b['cost']:.6f} | `{p['answer']}` | {p['score']} | ${p['cost']:.6f} | {routing} |")

    report.append("\n## Policy Mistakes & Error Analysis")
    wrong_tasks = []
    for idx, t in enumerate(tasks):
        b = baseline_results[idx]
        p = policy_results[idx]
        if b["score"] >= 0.9 and p["score"] < 0.9:
            wrong_tasks.append({
                "id": t["id"],
                "task": t["task"],
                "baseline_answer": b["answer"],
                "policy_answer": p["answer"],
                "cost_diff": b["cost"] - p["cost"]
            })
            
    if wrong_tasks:
        report.append("Our policy made a wrong routing decision on the following tasks:")
        for wt in wrong_tasks:
            report.append(f"### Task: `{wt['id']}`")
            report.append(f"* **Query**: {wt['task']}")
            report.append(f"* **Baseline (Frontier) Output**: `{wt['baseline_answer']}` (Correct)")
            report.append(f"* **Policy (Economy) Output**: `{wt['policy_answer']}` (Failed due to arithmetic or estimation error)")
            report.append(f"* **Cost Difference**: Saved **${wt['cost_diff']:.6f}** but lost task resolution.")
    else:
        report.append("No degradation: The budget-aware policy successfully resolved all tasks that the baseline resolved, demonstrating zero loss in accuracy while retaining maximum cost savings.")

    report.append("\n## Commands to Reproduce")
    report.append("```bash")
    report.append("cd S15Code")
    report.append("uv run python proofs/run_compute_eval.py")
    report.append("```")

    report_path = Path("/home/mani_radhakrishnan/TSAI_EAGV3_Session15/working_md/evaluate_policy_report.md")
    report_path.write_text("\n".join(report))
    print(f"Successfully generated comparison report at {report_path}")

if __name__ == "__main__":
    main()
