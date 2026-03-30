# Judge Prompts — Baseline Condition A

## How to use this file

For each block below:
1. Copy the **entire block** (from the horizontal rule to the next one).
2. Replace `[MODEL ANSWER HERE]` with the model's response from `03_results_log.md`.
3. Paste into a **new ChatGPT conversation** (start fresh each time — no cross-question context).
4. ChatGPT will reply with `HIT` or `MISS`.
5. Record the verdict in `05_scoring_sheet.csv` for that ID.

---

## Part A — Main Benchmark Sample

---

### Judge: Q15

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: 8 attempts.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q16

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: 30 days.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q17

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Every 5 seconds.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q21

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: order_stream

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q25

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: attemptCount is preserved.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q30

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: The migration guide and the PR/review material both state that only the exchange name changed and order.created stayed the same.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q33

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because Stripe signs the raw byte stream, so JSON normalization changes the payload and invalidates stripe-signature verification.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q38

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: The dispatcher treats 429 as retryable, so the worker marks the job failed and reschedules it with backoff unless it has already exhausted maxAttempts.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q42

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Only the exchange name changed from order_events to order_stream; the routing key, exchange type, durability, and payload shape stayed the same.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q50

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because some internal RabbitMQ setup and DLQ topology constants still use the legacy hyphenated name order-events, but the migration guide and release notes say external consumers should use order_stream.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q57

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: The completed attempt count.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q58

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Cursor-based pagination with cursor, nextCursor, and hasMore.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q67

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: v1.6.0

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q68

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: v1.6.1

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q76

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: order_stream was already canonical, but webhook retries could still be scheduled one slot too early because of the stale attemptCount bug.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q77

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: A proxy re-serialized the JSON body, which changed the raw bytes and broke Stripe signature verification.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q79

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: No. The routing key did not change, so the required update was the exchange name to order_stream.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q86

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because Stripe verifies the original raw bytes, and parsing plus re-stringifying changes the payload that the signature was computed over.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q90

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: The dispatcher marks 400 as non-retryable, and the worker treats non-retryable failures as dead immediately rather than scheduling backoff.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: Q91

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Retries were scheduled using the stale attempt count, and the fix was applied in the worker logic in src/jobs/webhookWorker.ts as described by issue and PR #241.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

## Part B — Bug/PR Benchmark

---

### Judge: BQ1

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because the worker rescheduled retries using the stale stored attemptCount instead of the completed attempt number, so backoff lagged behind the real attempt progression until the completedAttemptCount fix shipped in v1.6.1.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ2

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: The retry investigation says the mismatch came from when an attempt was treated as completed, issue and PR #241 describe the stale-count bug, the worker code shows completedAttemptCount, and backoff.ts preserves the jitter formula rather than changing it.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ3

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because Stripe verifies the original raw bytes, not a parsed JSON object. The proxy can re-serialize the body, changing the payload that stripe-signature covers, which is why the route keeps express.raw and the PR/review call out raw-body guardrails.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ4

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Issue/PR/review #233 plus the webhook docs describe the missing metadata.orderId path where delivery can succeed but the order stays PENDING, while issue #87 and the staging incident describe the distinct bad-signature path that returns an explicit webhook error.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ5

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because the missing metadata.orderId bug is a linkage failure, not a delivery failure: the webhook can be received successfully, but without the orderId it cannot map the payment back to an order and the order stays PENDING.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ6

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because the issue, PR #254, and its review explicitly frame the change as a defensive correlation-propagation patch, while the audit feature doc and audit service keep audit writes best-effort and non-blocking rather than required for business success.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ7

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because the drift issue, incident, PR #188, review thread, and ADR 004 all say the Swagger block came from an internal admin flow, while docs/integration/pagination.md remains the authoritative external contract.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ8

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because the migration changed the exchange name, not the routing key. The issue, PR, review, migration guide, release notes, and cutover runbook all show that a consumer still bound to order_events or a copied legacy internal name will miss traffic that now goes to order_stream.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ9

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because correlation IDs only preserved request-level log traceability. The investigation, issue #142, tracing notes, PR #143, and publisher code show that end-to-end distributed traces across RabbitMQ required OpenTelemetry context injection into message headers.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ10

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: Because the replay workflow is designed as an operational retry, not as a semantic reset. The runbook, admin route, PR/review #241, and WebhookJob model preserve attempt history so operators can replay a job without erasing provenance needed for debugging.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ11

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: PR #87 preserves two guardrails: keep express.raw on the Stripe route and do not add JWT auth there. Those constraints matter because raw-body handling is required for signature verification and adding auth is a separate but common webhook-breaking refactor mistake.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```

---

### Judge: BQ12

```
You are evaluating whether a model's answer contains a specific required fact.

Required fact: PR #233 intentionally did not add server-side recovery for missing metadata.orderId. Instead it made the integration contract explicit so teams can diagnose the difference between a linkage bug and a webhook-signature bug without changing runtime behavior.

Model answer: [MODEL ANSWER HERE]

Does the model answer contain the key information from the required fact?
Consider it a hit if the core factual claim is present, even if worded differently.
Consider it a miss if the model answer is vague, incorrect, or omits the key detail.

Reply with one word only: HIT or MISS
```
