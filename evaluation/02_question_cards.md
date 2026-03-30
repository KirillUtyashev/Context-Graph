# Question Cards — Baseline Condition A

## How to use this file

For each block below:
1. Start a **fresh Cursor chat** (no prior context).
2. Set the system prompt to the contents of `01_system_prompt.txt`.
3. Copy-paste **only the question text** as the user message.
4. Record the full model response in `03_results_log.md` (find the row by ID).
5. Close the chat and open a new one for the next question.

**Never give the agent a follow-up turn. One question → one response → done.**

---

## Part A — Main Benchmark Sample (20 questions from context_graph_retrieval_benchmark_v2)

---

### [IE-1] Q15 | information_extraction | 1 hop | LOW leakage

```
What is the default maximum number of attempts for an outbound webhook job?
```

---

### [IE-2] Q16 | information_extraction | 1 hop | LOW leakage

```
How long are dead webhook jobs retained before the TTL index purges them?
```

---

### [IE-3] Q17 | information_extraction | 1 hop | LOW leakage

```
How often does the webhook worker poll for runnable jobs?
```

---

### [IE-4] Q21 | information_extraction | 1 hop | LOW leakage

```
What is the current canonical RabbitMQ exchange name for downstream integrations after the migration?
```

---

### [IE-5] Q25 | information_extraction | 1 hop | LOW leakage

```
When operations manually retries a dead webhook job, what historical field is intentionally not reset?
```

---

### [MH-1] Q30 | multi_hop_retrieval | 3 hops | LOW leakage

```
Which artifacts explicitly prove that the order stream migration did not change the order.created routing key?
```

---

### [MH-2] Q33 | multi_hop_retrieval | 3 hops | LOW leakage

```
Why must a proxy forwarding Stripe webhooks avoid parsing and re-stringifying the body?
```

---

### [MH-3] Q38 | multi_hop_retrieval | 3 hops | LOW leakage

```
If a receiver returns HTTP 429 for an outbound webhook, what happens next in this system?
```

---

### [MH-4] Q42 | multi_hop_retrieval | 2 hops | LOW leakage

```
What exactly changed and what exactly stayed the same during the order_events to order_stream migration?
```

---

### [MH-5] Q50 | multi_hop_retrieval | 4 hops | LOW leakage

```
Why might the repo still mention order-events even though the external integration contract now says order_stream?
```

---

### [KU-1] Q57 | knowledge_update | 3 hops | LOW leakage

```
After the v1.6.1 fix, what count drives dead-letter decisions and backoff scheduling?
```

---

### [KU-2] Q58 | knowledge_update | 2 hops | LOW leakage

```
After the March 19 clarification, what remained the public pagination contract for /api/orders?
```

---

### [KU-3] Q67 | knowledge_update | 2 hops | LOW leakage

```
Which release first declared order_stream to be the canonical exchange name?
```

---

### [KU-4] Q68 | knowledge_update | 2 hops | LOW leakage

```
Which release fixed the webhook retry scheduling bug?
```

---

### [TR-1] Q76 | temporal_reasoning | 3 hops | LOW leakage

```
Between 2026-03-15 and 2026-03-20, what was the canonical exchange name and what retry bug was still present?
```

---

### [TR-2] Q77 | temporal_reasoning | 2 hops | LOW leakage

```
On 2026-03-05, why were staging Stripe webhooks failing even though the JSON content was semantically identical?
```

---

### [TR-3] Q79 | temporal_reasoning | 2 hops | LOW leakage

```
After 2026-03-12, if a consumer updated only the routing key but not the exchange name, would that fix the migration issue?
```

---

### [DP-1] Q86 | debugging_and_provenance | 3 hops | LOW leakage

```
Why does Stripe signature verification fail when the request body is parsed and then re-serialized before reaching the order service?
```

---

### [DP-2] Q90 | debugging_and_provenance | 3 hops | LOW leakage

```
Why does an outbound webhook that gets HTTP 400 from the receiver end up dead instead of retrying forever?
```

---

### [DP-3] Q91 | debugging_and_provenance | 3 hops | LOW leakage

```
Why were webhook retries arriving too fast in staging, and where was the fix applied?
```

---

## Part B — Bug/PR Benchmark (12 questions from context_graph_bug_pr_benchmark_subset)

All questions are LOW leakage, 5–6 hops. Near-total failure is expected.

---

### [BP-1] BQ1 | debugging_and_provenance | 6 hops | LOW leakage

```
Why were webhook retries arriving too quickly in staging before v1.6.1?
```

---

### [BP-2] BQ2 | debugging_and_provenance | 6 hops | LOW leakage

```
What evidence shows the webhook retry bug was in attempt bookkeeping rather than in the jitter formula itself?
```

---

### [BP-3] BQ3 | debugging_and_provenance | 6 hops | LOW leakage

```
Why can an edge proxy break Stripe webhook verification even if the forwarded JSON fields are identical?
```

---

### [BP-4] BQ4 | debugging_and_provenance | 6 hops | LOW leakage

```
Which artifact chain distinguishes the PaymentIntent metadata-linking bug from the Stripe signature-verification bug?
```

---

### [BP-5] BQ5 | debugging_and_provenance | 5 hops | LOW leakage

```
Why can a Stripe payment succeed while the order still remains PENDING with no signature error in the logs?
```

---

### [BP-6] BQ6 | debugging_and_provenance | 6 hops | LOW leakage

```
Why did the audit correlation hotfix improve traceability without making audit logging transactional?
```

---

### [BP-7] BQ7 | debugging_and_provenance | 6 hops | LOW leakage

```
Why should an engineer debugging external pagination behavior ignore the page-based Swagger block and trust the integration docs instead?
```

---

### [BP-8] BQ8 | debugging_and_provenance | 6 hops | LOW leakage

```
Why might a consumer with the correct routing key still miss order-created events after the exchange migration?
```

---

### [BP-9] BQ9 | debugging_and_provenance | 6 hops | LOW leakage

```
Why did the RabbitMQ trace-gap investigation conclude that correlation IDs alone were insufficient?
```

---

### [BP-10] BQ10 | debugging_and_provenance | 5 hops | LOW leakage

```
Why does manual webhook replay preserve attemptCount instead of resetting delivery history?
```

---

### [BP-11] BQ11 | debugging_and_provenance | 5 hops | LOW leakage

```
What design decision around the Stripe route is preserved in PR #87, and why is it important for future refactors?
```

---

### [BP-12] BQ12 | debugging_and_provenance | 5 hops | LOW leakage

```
What design decision did PR #233 deliberately not implement, and why?
```
