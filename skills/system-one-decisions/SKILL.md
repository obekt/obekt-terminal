---
name: system-one-decisions
description: "Fast typed AI decisions via TypeSafe Jev API."
version: 1.0.0
author: Hermes Agent
metadata:
  hermes:
    tags: [api, decisions, typesafe, agents]
---

# System One Decision Layer (TypeSafe Jev)

Use when a task needs a fast, cheap, structured judgment instead of an LLM
call: routing, screening, go/no-go, classification. Jev is a non-generating
decision model — send `state` + typed `questions`, get typed answers with
probabilities + confidence. Account key: `~/.config/typesafe/credentials.json`
(mode 600, field `api_key`; client also honors `TYPESAFE_API_KEY` env).
Docs index: `https://docs.typesafe.ai/llms.txt` (every page as .md).

**Client:** raw HTTP via `urllib` works fine (see `engine/okx_jev.py`'s `jev()`
function) — prefer that over installing the SDK in ephemeral runtimes. Wrap it
so it returns a flat `{name: value}` plus the raw probs/confidence and usage,
and append every call to an audit JSONL for later calibration.

**Pair it with a local reasoning LLM** for the escalation/generation side (this
repo uses a Qwen-3-class flash model on a self-hosted relay). The decision model
stays cheap and typed; the LLM only explains.

## 1. The API (verified live)

```
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <key>   Content-Type: application/json
{"state": <any JSON-able context>, "model": "jev-latest",
 "questions": {"name": {"type": "choice|score|noul", "instructions": "...", "criteria": ...}}}
```

- `choice` → `{choice, probabilities{opt:p}, confidence}`; `score` →
  `{score, legend, probabilities, confidence}` (criteria = ordered levels);
  `noul` → `{noul}` (0–1 truth probability, NO confidence field).
- All questions in one call run parallel against the same state; adding
  questions barely changes latency and never creates context-rot.
- Models: `GET /v1/models` → `jev-latest`, `jev-preview`. Pricing ≈$0.04/M
  input tokens — a 4-question/850-token decision round costs ~$0.00004,
  thousands of decisions per cent. Cost is effectively never the constraint.
- SDK: `pip install typesafe-sdk` (reads TYPESAFE_API_KEY). Raw HTTP via
  urllib works fine — prefer that to avoid installs in ephemeral runtimes.

## 2. Design rules (this is the actual skill)

- **Atomic questions only.** One well-scoped judgment per question — the kind
  a human makes in seconds. Decompose multi-factor judgments into separate
  questions and combine with weights IN CODE; never ask Jev to "rate X overall".
- **Code owns execution, Jev owns ambiguity.** Deterministic facts (fuel math,
  routes, deadlines, capacity) stay computed in code; only genuinely ambiguous
  calls (is this risky? which operation class fits?) go to Jev.
- **Confidence is the second axis (routing pattern).** `confidence` ≠
  `probabilities`. Three-band gate (calibrated by measurement, tune from the
  JSONL log, don't guess): act on Jev when `confidence >= 0.6`; in the
  0.35–0.65 mid-band escalate to the reasoning LLM — that band is the only
  place its ~23x price premium earns its keep; below that use the code
  fallback heuristic. Never force a low-confidence answer through. A safety
  question can use a different bar (e.g. threat noul >0.7 → unconditional
  flee/skip regardless of strategy).
- **On short-horizon action picks, gate on the probability MARGIN, not
  confidence.** Measured over 38 logged live trading picks: `confidence`
  saturated below the action bar on 35/38 calls even when the preference was
  clear (p(action)=0.74 vs p(no_action)=0.26 → confidence 0.48). Confidence
  measures certainty about the pick; the margin p(chosen)−p(alt) measures
  decision strength, and it tracks real edge. Gate: (margin ≥ ~0.25 OR
  confidence ≥ high bar) AND any noul floor. Before setting any bar, sweep
  candidate thresholds over the logged probabilities — every call already
  records them, so calibration is a replay, not a guess.
- **State should be dense facts, not prose.** Compact JSON of the decision-
  relevant fields (numbers, ids, counts). Criteria values may themselves be
  JSON structures.
- **Match the state horizon to the strategy premise.** If the thesis is
  "pullback inside a strong uptrend", a 30-minute facts window makes the
  premise unjudgeable — feed multi-timeframe numbers (e.g. 4h/7d change, days
  up of N, position in the wider range) and state the interpretation rule in
  the context prose (strong long-trend + mild short-dip = the setup; negative
  long-trend + short bounce = counter-trend, extra caution).
- **Measure the real chars-per-token of dense JSON state, don't assume.**
  Observed range on logged calls: 1.65–2.0 chars/token depending on content
  mix; budget prompt ceilings off the WORST observed ratio so the estimate
  over-reads (conservative), and log both your char count and the API's
  reported input_tokens every call to catch drift.
- **Log every decision round** (state, answers, chosen action, why) to a
  JSONL file — confidence calibration and post-hoc audit need the
  per-decision record, and it is the traceability artifact the user expects.

## 3. Integration skeleton (agent-loop pattern)

Each loop iteration: (1) gather live state via the target system's query API;
(2) one parallel Jev call with 3–5 atomic questions (strategy Choice, threat
Noul, urgency Score, a fit-check Noul); (3) gate answers in code
(`pick_action`: threat override → confidence bar → mapped action); (4) execute
the mapped mutation through the target system's rate limiter; (5) append to
JSONL log. Reference implementation (live-money crypto scalping, manage/decide
split with fee-wall and spread gates per venue): `engine/okx_jev.py` in this
repo — see the `okx-trading` skill for its operational rules.

## 4. Pitfalls (verified)

- **Stale-action loop**: if the mapped action's arrival effects are not
  executed once at the destination, the model keeps re-emitting the same
  transit decision every tick (it correctly sees "goods not aboard, still
  far away"). Every multi-step goal needs arrive-actions (dock/buy/complete)
  wired for the already-there case.
- **Missing state fields read as no-urgency**: Jev scored urgency 0 when fuel
  was shown as `217/None` (missing capacity field). Feed real numbers or
  omit the field entirely from the state rather than emitting `None`/nulls
  that masquerade as data.
- **Noul has no confidence** — threshold the noul value itself; Choice/Score
  carry a separate `confidence` for the gate. Don't compare a noul to a
  choice-confidence bar.
- **Never ask the model what code can compute.** The pilot kept answering
  "acquire goods" while the cargo manifest already held the mission goods —
  the model answers the question asked, it doesn't cross-check facts against
  inventories. Put a code-side readiness check (`mission_ready()`) ahead of
  the gate so computable facts override market-leaning strategy answers.
- **Own-published tables pollute your own gate.** When a pipeline appends a
  Jev-generated table to content and later gates similar content, gate the
  ORIGINAL content first — the appended machine table shifts spam/quality
  scores. Order: gate → augment → publish.

## 4b. Job/task selection pattern (feasibility filter + EV pick)

General form for "agent picks a job" (contracts, queues, listings):
(1) CODE filters candidates by hard capability constraints (what the agent
*can* do — drops combat tasks for an unarmed hauler, paid-only, has-destination);
(2) Jev picks exactly ONE via `choice` over the filtered dict, with the
agent's traits and LOSS HISTORY in the state — put losses in the facts ("51
units lost to pirates") and picks shift measurably toward survivability;
(3) fallback if `confidence < 0.45` or pick invalid: argmax on a simple
deterministic metric. Log the pick and its `why` source.

## 4c. Evidence discipline before publishing claims

When you A/B a typed decision model against a reasoning LLM, measure with a real
harness (OpenAI-compatible endpoint, configurable model/base/key) and record
rounds-to-stability. In our own small comparison the typed model was ~23x
cheaper and 1.6–3.1x faster, while the LLM kept better RAW calibration — but
action-level outcomes were at parity. Rule: never publish a performance claim
without a harness run first, and publish the findings that DON'T flatter your
tool too (calibration loss, small-n caveats); that honesty is what makes the
claim credible.
- **Don't parse free text** out of Jev — it returns no prose; if a task
  needs generation it needs a different model.
- **Garbage state is confidently wrong**: Jev answers confidently on whatever
  facts you give. The code's job is feeding it true, complete, compact state
  — a confidently-worded answer is not validation of bad inputs.
- **Deadlines go in code clocks, never in the question.** Asked at 18:55 whether
  to hold a position, the model weighs only the facts you gave — it will
  approve crossing a close/EOD/weekend boundary it was never told about as a
  hard constraint. Force the boundary in code; only ask the model about the
  fuzzy part inside it.
- **Abstention is valuable output.** When each action carries a fixed cost
  (fees, rate limits, social capital), a high-confidence `no_trade` is the
  system working, not idling — every skipped action saves the cost. Don't
  lower gates to force activity when measured volatility says most setups
  can't clear the cost wall.
