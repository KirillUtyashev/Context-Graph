# Results Log — Baseline Condition A

## Instructions

After running each question in Cursor:
- Paste the full model response into the **Model Answer** cell for that row.
- Leave **Judge Verdict** and **Miss Class** blank — fill those in during the judging phase.

---

## Part A — Main Benchmark Sample

| ID | Label | Type | Hops | Question (short) | Model Answer | Judge Verdict | Miss Class |
|----|-------|------|------|------------------|--------------|---------------|------------|
| Q15 | IE-1 | information_extraction | 1 | Max webhook job attempts? | | | |
| Q16 | IE-2 | information_extraction | 1 | Dead webhook TTL? | | | |
| Q17 | IE-3 | information_extraction | 1 | Worker poll interval? | | | |
| Q21 | IE-4 | information_extraction | 1 | Canonical RabbitMQ exchange name? | | | |
| Q25 | IE-5 | information_extraction | 1 | Field not reset on manual replay? | | | |
| Q30 | MH-1 | multi_hop_retrieval | 3 | Proof routing key didn't change in migration? | | | |
| Q33 | MH-2 | multi_hop_retrieval | 3 | Why avoid re-serializing Stripe webhook body? | | | |
| Q38 | MH-3 | multi_hop_retrieval | 3 | HTTP 429 from outbound webhook receiver — what happens? | | | |
| Q42 | MH-4 | multi_hop_retrieval | 2 | What changed/stayed same in order_events→order_stream migration? | | | |
| Q50 | MH-5 | multi_hop_retrieval | 4 | Why might repo still mention order-events? | | | |
| Q57 | KU-1 | knowledge_update | 3 | What drives backoff/DLQ after v1.6.1 fix? | | | |
| Q58 | KU-2 | knowledge_update | 2 | Pagination contract after Mar 19 clarification? | | | |
| Q67 | KU-3 | knowledge_update | 2 | Which release made order_stream canonical? | | | |
| Q68 | KU-4 | knowledge_update | 2 | Which release fixed webhook retry bug? | | | |
| Q76 | TR-1 | temporal_reasoning | 3 | State between Mar 15–20 (exchange name + retry bug)? | | | |
| Q77 | TR-2 | temporal_reasoning | 2 | Why staging Stripe webhooks failed on Mar 5? | | | |
| Q79 | TR-3 | temporal_reasoning | 2 | Updating routing key only after Mar 12 — would that fix migration? | | | |
| Q86 | DP-1 | debugging_and_provenance | 3 | Why does re-serialized body break Stripe sig verification? | | | |
| Q90 | DP-2 | debugging_and_provenance | 3 | Why does HTTP 400 from receiver end in dead (not retry)? | | | |
| Q91 | DP-3 | debugging_and_provenance | 3 | Why retries too fast in staging, where was fix? | | | |

---

## Part B — Bug/PR Benchmark

| ID | Label | Type | Hops | Question (short) | Model Answer | Judge Verdict | Miss Class |
|----|-------|------|------|------------------|--------------|---------------|------------|
| BQ1 | BP-1 | debugging_and_provenance | 6 | Why were retries too fast before v1.6.1? | | | |
| BQ2 | BP-2 | debugging_and_provenance | 6 | Evidence bug was in attempt bookkeeping not jitter formula? | | | |
| BQ3 | BP-3 | debugging_and_provenance | 6 | Why does edge proxy break Stripe sig even if JSON identical? | | | |
| BQ4 | BP-4 | debugging_and_provenance | 6 | Artifact chain distinguishing metadata-link bug vs sig bug? | | | |
| BQ5 | BP-5 | debugging_and_provenance | 5 | Stripe payment succeeds but order stays PENDING, no sig error? | | | |
| BQ6 | BP-6 | debugging_and_provenance | 6 | Audit correlation hotfix improved traceability without transactions? | | | |
| BQ7 | BP-7 | debugging_and_provenance | 6 | Why ignore Swagger block, trust integration docs for pagination? | | | |
| BQ8 | BP-8 | debugging_and_provenance | 6 | Correct routing key but still misses events after migration? | | | |
| BQ9 | BP-9 | debugging_and_provenance | 6 | Why was correlation ID alone insufficient for RabbitMQ tracing? | | | |
| BQ10 | BP-10 | debugging_and_provenance | 5 | Why manual replay preserves attemptCount? | | | |
| BQ11 | BP-11 | debugging_and_provenance | 5 | Design decision in PR #87 and why it matters? | | | |
| BQ12 | BP-12 | debugging_and_provenance | 5 | What did PR #233 deliberately NOT implement, and why? | | | |

---

## Detailed Answers

Paste full model responses below. Use the ID as a header.

### Q15
The default maximum number of attempts for an outbound webhook job is 8, as set on the WebhookJob model and when enqueueing jobs in webhookService (maxAttempts defaults to 8 when not specified).

Cache Read
0
Cache Write
0
Input
26,696
Output
281
Total
26,977

### Q16
Dead webhook jobs are kept for 30 days before MongoDB’s TTL index removes them (the index uses updatedAt with expireAfterSeconds set to 30 days for status: "dead").

Cache Read
0
Cache Write
0
Input
25,931
Output
311
Total
26,242

### Q17
The webhook worker runs its poll cycle every 5 seconds (POLL_INTERVAL_MS = 5_000 in src/jobs/webhookWorker.ts), so runnable jobs are checked at that interval after each batch finishes.

Cache Read
0
Cache Write
0
Input
28,160
Output
402
Total
28,562

### Q21
The current canonical RabbitMQ exchange name for downstream integrations after the migration is order_stream (replacing the former order_events name, as stated in docs/migrations/2026-03-12-order-stream-rollout.md and docs/releases/2026-03-15-v1.6.0.md).

Cache Read
0
Cache Write
0
Input
26,584
Output
331
Total
26,915

### Q25
attemptCount is intentionally not reset when operations manually retries a dead webhook job, so prior delivery attempts stay visible for audit and debugging.

Cache Read
0
Cache Write
0
Input
27,356
Output
324
Total
27,680

### Q30
The migration guide (docs/migrations/2026-03-12-order-stream-rollout.md) and the rollout PR and review (history/pull-requests/176-order-stream-rollout.md, history/reviews/176-order-stream-rollout-review.md) are the artifacts that explicitly state only the exchange name changed and order.created stayed the same.

Cache Read
0
Cache Write
0
Input
25,813
Output
438
Total
26,251

### Q33
A proxy must forward Stripe’s webhook body unchanged because signature verification uses the exact raw bytes Stripe signed, and parsing and re-stringifying JSON can alter those bytes (whitespace, key order, encoding), invalidating the signature.

Cache Read
0
Cache Write
0
Input
11,461
Output
147
Total
11,608

### Q38
The dispatcher treats HTTP 429 as retryable, so the webhook worker marks the job as failed, records the error, bumps attemptCount, and reschedules delivery at nextRunAt using the backoff schedule until it succeeds or hits maxAttempts (default 8), at which point it moves to dead.

Cache Read
0
Cache Write
0
Input
29,259
Output
484
Total
29,743

### Q42
Only the RabbitMQ exchange name changed from order_events to order_stream, while the exchange type (topic), durability (true), routing key (order.created), and event payload shape all stayed the same.

Cache Read
0
Cache Write
0
Input
27,867
Output
301
Total
28,168

### Q50
The repo can still say order-events because names in code, env vars, or infra often lag a published order_stream contract during a migration, or because order-events remains the actual wire/topic identifier while order_stream is only the external product name in the integration spec.

Cache Read
0
Cache Write
0
Input
11,463
Output
337
Total
11,800

### Q57
After v1.6.1, completed attempt count (completedAttemptCount) drives both dead-letter decisions and backoff scheduling (replacing the stale attemptCount used before the fix).

Cache Read
0
Cache Write
0
Input
26,972
Output
368
Total
27,340

### Q58
After the March 19 clarification, the public pagination contract for /api/orders stayed cursor-based pagination, using cursor, nextCursor, and hasMore (as documented in the integration pagination spec, not the page-based Swagger block).

Cache Read
0
Cache Write
0
Input
26,619
Output
259
Total
26,878

### Q67
<!-- paste here -->

### Q68
<!-- paste here -->

### Q76
<!-- paste here -->

### Q77
<!-- paste here -->

### Q79
<!-- paste here -->

### Q86
<!-- paste here -->

### Q90
<!-- paste here -->

### Q91
<!-- paste here -->

### BQ1
<!-- paste here -->

### BQ2
<!-- paste here -->

### BQ3
<!-- paste here -->

### BQ4
<!-- paste here -->

### BQ5
<!-- paste here -->

### BQ6
<!-- paste here -->

### BQ7
<!-- paste here -->

### BQ8
<!-- paste here -->

### BQ9
<!-- paste here -->

### BQ10
<!-- paste here -->

### BQ11
<!-- paste here -->

### BQ12
<!-- paste here -->
