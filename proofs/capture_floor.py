import json
import os
import sqlite3
import urllib.request
import urllib.parse
from pathlib import Path

PROMPTS = [
    "What is the capital of Japan? Reply with the name only.",
    "What is 15% of 300? Reply with the number only.",
    "Who wrote the novel '1984'? Reply with the author name only.",
    "What is the boiling point of water in Celsius? Reply with the temperature only."
]

def make_request(url, method="GET", data=None, headers=None):
    headers = headers or {}
    req_data = None
    if data:
        req_data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"Error calling {url}: {e}")
        return None

def query_ledger(run_id):
    db_path = Path(os.path.expanduser("~/.glc/gateway.sqlite"))
    if not db_path.exists():
        return f"Database not found at {db_path}"
    
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # The agent's run_id matches the session column in the gateway calls table
        cursor.execute("SELECT * FROM calls WHERE session = ?", (run_id,))
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return "No matching rows found in calls table"
        
        result = []
        for idx, row in enumerate(rows):
            row_dict = {key: row[key] for key in row.keys()}
            result.append(f"Row {idx+1}: " + json.dumps(row_dict, indent=2))
        return "\n".join(result)
    except Exception as e:
        return f"Error querying database: {e}"

def main():
    print("Starting automated runs to capture the floor details...")
    report = []
    
    for idx, prompt in enumerate(PROMPTS):
        print(f"Running task {idx+1}/{len(PROMPTS)}: {prompt}")
        
        # 1. Trigger agent run
        run_url = "http://127.0.0.1:8113/v1/agent/runs"
        payload = {
            "prompt": prompt,
            "budget": 0.05,
            "tenant_id": "course",
            "project_id": "s15",
            "user_id": "student-01",
            "agent_id": "assistant"
        }
        run_res = make_request(run_url, method="POST", data=payload)
        if not run_res:
            print("Failed to run agent task.")
            continue
        
        run_id = run_res.get("run_id")
        status = run_res.get("status")
        answer = run_res.get("answer")
        events = run_res.get("events", [])
        
        print(f"Run completed. ID: {run_id}, Status: {status}")
        
        # 2. Get trace details
        trace_url = f"http://127.0.0.1:8113/v1/agent/runs/{run_id}/trace"
        trace_res = make_request(trace_url)
        trace_id = "N/A"
        if trace_res:
            trace_ids = trace_res.get("totals", {}).get("trace_ids", [])
            if trace_ids:
                trace_id = trace_ids[0]
        
        # 3. Query ledger database
        ledger_data = query_ledger(run_id)
        
        # Format events trace
        event_trace_md = []
        for ev in events:
            node_str = f" (Node: {ev['node_id']})" if ev.get("node_id") else ""
            event_trace_md.append(f"- Seq {ev['sequence']}: {ev['kind']}{node_str}")
        event_trace_str = "\n".join(event_trace_md)
        
        # Extract model/tier chosen
        tier_chosen = "N/A"
        model_chosen = run_res.get("model") or "N/A"
        provider_chosen = run_res.get("provider") or "N/A"
        
        # Compile run section
        run_md = f"""### Run {idx+1}: {prompt}
* **Run ID**: `{run_id}`
* **Final Answer**: `{answer}`
* **Model/Provider Chosen**: `{model_chosen}` (via `{provider_chosen}`)
* **OTel Trace ID**: `{trace_id}`

#### Ordered Event Trace:
{event_trace_str}

#### Ledger Rows from gateway.sqlite:
```json
{ledger_data}
```
---
"""
        report.append(run_md)
        
    out_path = Path("/home/mani_radhakrishnan/TSAI_EAGV3_Session15/working_md/floor_reproduction.md")
    out_path.write_text("\n".join(report))
    print(f"Floor reproduction report successfully generated at {out_path}!")

if __name__ == "__main__":
    main()
