"""Utility: score candidate IT-ticket triage prompts by the same hash the
simulated gateway uses, so we can pick a balanced 18-task set for Part 2.

Not part of the shipped harness. Run: ``uv run python proofs/_pick_tickets.py``.
"""
from __future__ import annotations

import hashlib


def needed(prompt: str) -> int:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    return 200 + int(digest, 16) % 3600


CANDIDATES = [
    # trivial-target: single obvious cause, one first step
    "Ticket: user cannot print to LaserJet-14. Return JSON {category,severity,first_step}.",
    "Ticket: password reset requested by staff@acme.com. Return JSON {category,severity,first_step}.",
    "Ticket: user cannot access shared drive S:. Return JSON {category,severity,first_step}.",
    "Ticket: Outlook keeps prompting for password after MFA reset. Return JSON {category,severity,first_step}.",
    "Ticket: monitor on desk 42 shows no signal. Return JSON {category,severity,first_step}.",
    "Ticket: laptop battery not charging on dock model D-9. Return JSON {category,severity,first_step}.",
    "Ticket: Zoom microphone not detected after Windows update. Return JSON {category,severity,first_step}.",
    "Ticket: new starter needs GitHub Enterprise seat provisioned. Return JSON {category,severity,first_step}.",
    "Ticket: user locked out after 5 failed attempts on VPN. Return JSON {category,severity,first_step}.",
    "Ticket: printer PR-07 shows toner low warning, still printing. Return JSON {category,severity,first_step}.",
    # moderate-target: some diagnosis
    "Ticket: engineer reports intermittent 502 from api.internal on Chrome only, Safari fine. Return JSON {category,severity,first_step}.",
    "Ticket: finance team says Excel crashes when they open the month-end pivot workbook, other files open fine. Return JSON {category,severity,first_step}.",
    "Ticket: three users in Berlin office report SSO to Jira loops back to login after upgrade last night. Return JSON {category,severity,first_step}.",
    "Ticket: sales laptop reports drive encryption stuck at 78% for 4 hours, laptop hot. Return JSON {category,severity,first_step}.",
    "Ticket: Slack notifications missing for one channel after user was re-added. Return JSON {category,severity,first_step}.",
    "Ticket: developer says local Docker desktop uses 100% CPU on idle after last week's update. Return JSON {category,severity,first_step}.",
    "Ticket: shared calendar invites from CEO's assistant show wrong timezone for London recipients. Return JSON {category,severity,first_step}.",
    "Ticket: two-factor SMS not arriving for user on new SIM, other apps OK. Return JSON {category,severity,first_step}.",
    "Ticket: nightly S3 backup job reports 0 bytes transferred but exit code 0 for the last 3 nights. Return JSON {category,severity,first_step}.",
    "Ticket: prod alert channel silent for 6h; heartbeat monitor still green. Return JSON {category,severity,first_step}.",
    # hard-target: ambiguous or cross-team
    "Ticket: customers on the /checkout page in Safari 17.4 report an intermittent hang for 8-15 seconds; New Relic p95 shows a spike at the same timestamps but only from one region, and rolling back the payment SDK does not remove the spike. Return JSON {category,severity,first_step}.",
    "Ticket: since a firewall rule change on Tuesday, the analytics ingestion pipeline drops roughly 2% of events, but only ones tagged mobile=true, and only during 10:00-11:00 UTC. There are no error logs on the ingest side. Return JSON {category,severity,first_step}.",
    "Ticket: an internal ML training job started OOM-killing overnight after we upgraded the CUDA driver on the GPU pool, but only when three specific jobs run concurrently, and only on the A100 nodes. Return JSON {category,severity,first_step}.",
    "Ticket: a customer reports our SSO SAML assertion fails signature validation on their IdP, but only for users provisioned after 1 August, and other tenants on the same certificate work. Return JSON {category,severity,first_step}.",
    "Ticket: the mobile app's push notifications arrive twice for Android users on carrier A, once for iOS, and not at all for Android on carrier B, following the last certificate rotation. Return JSON {category,severity,first_step}.",
    "Ticket: an on-call engineer paged us because the auto-scaler is thrashing between 6 and 9 nodes every 90 seconds, but only in eu-west-2, only on Sunday nights, and CPU is nowhere near either threshold. Return JSON {category,severity,first_step}.",
    "Ticket: our staging Postgres reports duplicate primary-key errors on a table that has an autoincrement column, but only on inserts routed through the read-replica connection pool, which claims to be read-only. Return JSON {category,severity,first_step}.",
    "Ticket: a batch of customer invoices generated between 03:12 and 03:47 UTC last night has the wrong VAT rate for the Republic of Ireland, but only when the customer's billing address was updated within 24 hours of invoice generation. Return JSON {category,severity,first_step}.",
    "Ticket: an internal Kafka topic loses roughly 1 message per 10k during producer restarts, but only when the message key is exactly 16 bytes and the producer is version 3.4.1 talking to a 3.6 broker. Return JSON {category,severity,first_step}.",
    "Ticket: a user reports that Excel formulas referencing external workbooks now return #REF errors on file open, but only for files stored on a specific SharePoint site collection that was migrated last week. Return JSON {category,severity,first_step}.",
]


TRIVIAL_MAX = 1500     # economy ladder rung's max_tokens
MODERATE_MAX = 2600    # standard ladder rung's max_tokens
# frontier rung >= 3800 covers every hash


def bucket_of(n: int) -> str:
    if n <= TRIVIAL_MAX:
        return "trivial"
    if n <= MODERATE_MAX:
        return "moderate"
    return "hard"


def main():
    rows = [(needed(c), c) for c in CANDIDATES]
    rows.sort(key=lambda r: r[0])
    per_bucket: dict[str, list[tuple[int, str]]] = {"trivial": [], "moderate": [], "hard": []}
    for n, c in rows:
        per_bucket[bucket_of(n)].append((n, c))

    print("=== all candidates ===")
    print(f"{'needed':>7}  {'bucket':<8}  prompt")
    print("-" * 110)
    for n, c in rows:
        print(f"{n:>7}  {bucket_of(n):<8}  {c[:90]}")
    print(f"\ntotals: trivial={len(per_bucket['trivial'])} "
          f"moderate={len(per_bucket['moderate'])} hard={len(per_bucket['hard'])} (of {len(rows)})\n")

    print("=== picked 6/6/6 ===")
    picked: list[tuple[int, str, str]] = []
    for b in ("trivial", "moderate", "hard"):
        for n, c in per_bucket[b][:6]:
            picked.append((n, b, c))
    for n, b, c in picked:
        print(f"{n:>7}  {b:<8}  {c[:90]}")
    print(f"\npicked total: {len(picked)}")


if __name__ == "__main__":
    main()
