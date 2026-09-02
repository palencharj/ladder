# Cost model

Every number here was measured, not estimated. Where a figure is
hardware-specific it says so, and `scripts/bench.py` reproduces it on your box.

## Rate card

Anthropic first-party API rates, USD per million tokens.

| Rung | Tier | Model | Input | Output | Context |
|---|---|---|---|---|---|
| 0 | local | `qwen3-coder:30b` | free | free | 256K |
| 1 | haiku | `claude-haiku-4-5` | $1.00 | $5.00 | 200K |
| 2 | sonnet | `claude-sonnet-5` | $3.00 | $15.00 | 1M |
| 3 | sonnet-high | `claude-sonnet-5` | $3.00 | $15.00 | 1M |
| 4 | opus | `claude-opus-5` | $5.00 | $25.00 | 1M |
| 5 | fable | `claude-fable-5` | $10.00 | $50.00 | 1M |

Rungs 2 and 3 are the same model at different `effort`. Buying more thinking on
Sonnet is much cheaper than buying a bigger model, which is why the ladder has
that rung at all — it is the cheapest way to add care before escalating to Opus.

Sonnet 5 has an introductory rate of $2.00/$10.00 through 2026-08-31, so real
spend before that date is below what the dashboard estimates. The estimate is
deliberately conservative.

## The `claude -p` overhead trap

This is the single most important cost fact in the project.

Rungs 1–5 use `claude -p` unless an `ANTHROPIC_API_KEY` is set. That authenticates against a Claude Code subscription — convenient,
no key to provision — but every invocation reloads the entire Claude Code
harness: system prompt, all built-in tool definitions, settings, and project
context.

Measured on the development machine, 2026-08-24, asking Haiku 4.5 to reply with
a single word:

| Invocation | cache write | cache read | output | cost |
|---|---|---|---|---|
| `claude -p` (first call) | 34,054 | 0 | 45 | $0.0683 |
| `claude -p` (warm cache) | 9,976 | 24,909 | 50 | $0.0227 |
| `claude -p` + `--setting-sources "" --strict-mcp-config` | 25,404 | 0 | 53 | **$0.0517** |

Three things to take from that table.

**The overhead is irreducible through the CLI.** The "lean" invocation stripped
settings, MCP config, and the system prompt, and still carried 25k tokens of
built-in tool definitions. It also broke the cache prefix, so a single lean call
cost *more than double* the warm normal one. There is no flag that makes
`claude -p` cheap for small tasks.

**On a prepaid plan this is allowance, not money.** Most teams run Claude Code
on a subscription, where nobody receives a bill and the binding constraint is
usage allowance. That makes the overhead sharper, not softer: ~35k tokens are
charged *per invocation* regardless of task size, so a hundred one-line jobs
spend ~3.5M tokens of quota before any real work happens. Dollar figures in this
tool are notional — what the work would have cost at API rates — while
`ladder_report` measures the thing that actually binds.

**The overhead is per call, not per task, so batch.** This is the single biggest
lever available on a subscription. Ten tasks in one invocation spend the fixed
cost once instead of ten times. Measured here: six classifications answered in
**one** `claude -p` call, 28,416 tokens spent against roughly 210,000 for six
separate calls — **175,000 tokens of allowance saved**, all six answers correct.

```
ladder_swarm(tasks=[...], batch=true)
```

Batching only groups tasks sharing kind, verify, max_tokens and model, and only
those with no escalation headroom and no adjudication, since a batch answers at
exactly one rung. If the reply does not parse into exactly one answer per task
it falls back to individual calls rather than risk handing task 3's answer to
task 4.

The CLI engine is also the right tool for *tool-using* jobs: it gets file
editing, bash, and search for free, which the raw API engine does not.

## What local inference actually costs you

Nothing in dollars. The cost is wall-clock, and it is hardware-dependent.

Measured on an Intel Core Ultra 7 265U (12C/14T, 15W class, **no discrete
GPU**), 64 GB DDR5-5600 (~90 GB/s theoretical), Ollama 0.32.14, CPU-only:

| Model | Size | Active params | Generation | Prefill |
|---|---|---|---|---|
| `qwen2.5-coder:3b` (dense) | 1.9 GB | ~3B | 13.3 tok/s | 60.8 tok/s |
| `qwen2.5-coder:7b` (dense) | 4.7 GB | ~7B | 6.6 tok/s | 20.4 tok/s |
| `qwen3-coder:30b` (MoE) | 18 GB | ~3B | 11.5 tok/s | 31.9 tok/s |

On short outputs with the model already resident, both the 3B and the 30B
answer a two-token classification in **0.3 s**, at 37.8 and 35.4 tok/s
respectively.

> **These figures replace earlier ones.** An initial round of measurement
> reported 3.2–5.4 tok/s and claimed the 7B was no faster than the 30B. Both
> were artefacts: the runs were taken during sustained load with the machine
> already throttling, and a separate "3B is 20× faster" claim turned out to be
> measuring model *load* time, not generation. The corrected numbers are above.

Three findings that should change how you use rung 0:

**Speed tracks _active_ parameters, which is the signature of a bandwidth
limit.** The 7B is roughly half the 3B, matching its parameter ratio. The 30B
mixture-of-experts is nearly as fast as the 3B despite being ten times the size
on disk, because it activates only ~3B parameters per token. Generation is
bound by how many bytes must cross the memory bus, not by arithmetic. This is
also why an NPU does not help: it shares the very same system RAM.

**Residency dominates perceived speed, not tokens per second.** A cold 30B
costs ~33 s to page 18 GB in from disk; warm, the same request takes 0.3 s. A
hundredfold difference that has nothing to do with generation rate. Ladder
therefore sends `keep_alive` (default 30 minutes, `LADDER_KEEP_ALIVE` to
change) — Ollama's own 5-minute default is too short for bursty use, where
stepping away for a coffee means the next job pays the full reload.

**Throughput still degrades under sustained load.** The 30B measured 11.5 tok/s
on a cool machine and ~3.2 tok/s after minutes of continuous inference —
roughly a 3.5× fall, consistent with clock throttling on a 15 W part. Long
swarm runs get slower as they go, so a batch timed from its first few jobs will
finish later than you predict.

**Local concurrency does not increase generation throughput.** Measured on the
same machine:

| Concurrency | Aggregate generation | Prefill |
|---|---|---|
| serial | 5.6 tok/s | 17.7 tok/s |
| 2-way | 5.3 tok/s | 470 tok/s |
| 4-way | 5.8 tok/s | 425 tok/s |

Total generation throughput is flat at ~5.5 tok/s no matter how many requests
run at once — one stream already saturates the memory bus, so parallel jobs
simply take turns. That is why rung 0 caps at 2 while Haiku caps at 12.

Prefill behaves in the opposite way, jumping roughly 25× under concurrency,
because prompt processing is compute-bound and Ollama batches it across
requests. The practical consequence: **local fan-out is worthwhile for
prompt-heavy, output-light work** — classification, triage, extraction,
"does this file do X" — and worthless for anything that generates many tokens.
If your swarm is mostly `classify` and `triage`, raising the rung-0 cap is
reasonable. If it is generating code, leave it at 2.

Run `python scripts/bench.py --concurrency 1 2 4` for your own figures.

On a machine with a discrete GPU these numbers change completely and rung 0 may
become fast enough for interactive use. Measure before assuming.

### Practical reading

With the model resident, a short answer comes back in well under a second, and
a 200-token docstring takes roughly twenty seconds at 11 tok/s. That is usable
interactively for small outputs, and still batch-shaped for anything long —
especially since sustained runs throttle toward ~3 tok/s.

The thing to avoid is a *cold* model: the first request after an idle period
pays the full reload, which is why `keep_alive` defaults to 30 minutes.

### Timeouts at rung 0

Local deadlines are derived from `max_tokens`, not fixed. A fixed timeout is
wrong here because how long a job takes depends entirely on how many tokens you
asked for and how slow the machine is.

This mattered more than it sounds. The original fixed 900s deadline, combined
with the default `max_tokens` of 8000, meant every rung-0 job that genuinely
filled its budget timed out — 8000 tokens at 3.2 tok/s needs about 2500s. The
router read the timeout as a failure and escalated to a **paid** rung. The free
tier was quietly spending money on work the local model would have finished
given another twenty minutes.

The deadline is now `max_tokens / 2.0 + 120s`, clamped to [300s, 7200s]. The
2.0 tok/s floor is deliberately slower than any machine measured — being too
generous only delays a failure, while being too tight costs real money. The
constants are at the top of `ladder/engines/ollama_engine.py`; raise
`MIN_TOKENS_PER_SEC` if your hardware is faster and you want quicker failures.

A practical consequence: at default `max_tokens`, a rung-0 job can legitimately
run for over an hour. That is the free tier working as intended, not a hang.
Pass a smaller `max_tokens` when you want a bound — it tightens the deadline
proportionally.

### Truncation is answered with budget, not a bigger model

If an attempt stops because it hit `max_tokens`, Ladder retries at the **same
rung** with double the budget rather than escalating.

Escalating would be strictly wrong. `max_tokens` is one value for the whole
job, so a dearer model handed the same cap hits exactly the same wall -- you
would spend a rung to reproduce the failure. Truncation is a budget problem and
only more budget fixes it.

The budget doubles up to a 32,000-token ceiling. An answer still truncated
there is rejected rather than returned, because a half-finished answer reported
as success is the same silent-wrongness trap that structural verifiers fall
into.

Same-rung retries are recorded in the escalation trail but do **not** count as
escalations, since that number is what you tune `TASK_RUNGS` against and
inflating it would point the tuning the wrong way.

## Picking a smaller local model per task

Rung 0's default is the 30B, because for code generation it is no slower than a
7B and considerably smarter. For **short-output** work that calculus inverts
completely -- generation time dominates, and a 3B finishes far sooner.

An earlier version of this section claimed a 20× speedup for the 3B, from
1.7 s against 33.8 s on one classification. That was wrong: the 33.8 s was
almost entirely the cost of paging an 18 GB model in from disk. With both
models resident the same request takes 0.3 s on either, and generation runs at
37.8 tok/s on the 3B against 35.4 on the 30B — within 7%.

| Model | Warm, 2-token answer | Generation | Disk | RAM held |
|---|---|---|---|---|
| `qwen2.5-coder:3b` | 0.3 s | 37.8 tok/s | 1.9 GB | 2.2 GB |
| `qwen3-coder:30b` | 0.3 s | 35.4 tok/s | 18 GB | 19 GB |

So the 3B's real advantages are **load time and memory footprint**, not
throughput. Prefer it when RAM is contended, when the model will often be cold,
or on a machine that cannot hold 19 GB resident. Otherwise the 30B is the
better default: same speed once warm, considerably more capable.

Pass `model` to switch per job:

```
ladder_run(prompt="...", kind="classify", model="qwen2.5-coder:3b", max_rung=0)
```

The override applies to the **starting rung only** and keeps that rung's
engine, pricing, and concurrency budget. If the job escalates, each rung above
uses its own standard model -- the override describes this task, not the ladder.

Rule of thumb: under ~50 tokens of expected output, use the 3B. Above that,
the extra capability of the 30B is worth the wait, since you are waiting either
way.

Pull it with `ollama pull qwen2.5-coder:3b` (1.9 GB).

## Is it worth it?

`ladder_report` (or `/api/report`, or the top panel of the dashboard) answers
that with a verdict: **worth-it**, **marginal**, **not-worth-it**, or
**insufficient-data**.

It is built to be able to say no. Three deliberate choices keep it honest,
because a savings dashboard that can only report savings is marketing:

**Avoided spend is priced at the cheapest paid tier, not the dearest.** Work
that finished free at rung 0 is valued at Haiku rates, because Haiku is what
you would actually have used. Pricing a docstring against Fable 5 is how tools
like this flatter themselves. (`ladder_stats` still shows the top-rung
comparison, clearly labelled as an upper bound — treat `ladder_report` as the
real number.)

**Failed cheap attempts are subtracted.** Trying cheap and missing is not free.
`wasted_spend` is money spent on attempts that did not produce the final
answer, and `net_saving` is `avoided_spend - wasted_spend`. It can be negative.

**The free tier is charged for wall clock.** Free is only free if the work ran
unattended. The report divides net saving by hours of local compute to get an
implied hourly rate and judges that. A rate of a few cents per hour means rung
0 is not paying for anyone's time, however free the tokens were.

It also reports **first-try rate** per task kind — the fraction of jobs that
succeed at their starting rung. That is the single best signal for retuning
`TASK_RUNGS`: a kind that almost always escalates is starting too low and you
are paying for a doomed attempt every time.

Per-user rows let a team see who is using it and what each person's mix of free
and paid work looks like. Identity comes from `LADDER_USER`, falling back to the
OS username.

### Reasoning models are wrong for rung 0

A candidate can match the incumbent's architecture exactly and still lose badly.
Measured against `qwen3-coder:30b` on six tasks drawn from real usage:

| model | correct | total | tok/s |
|---|---|---|---|
| `qwen3-coder:30b` | **6/6** | **23.7 s** | 10.7 |
| `nemotron-3.5-lightning:30b-a3b` | 3/6 | 333.5 s | 6.5 |

Same 30B total, same 3B active, and per-token speed did land in the same
ballpark — the active-parameter rule held. It lost **14x on wall clock** anyway,
because it is a reasoning model: it emits 10-50x more tokens to answer the same
question. Given a generous 2,500-token budget it spent **9,705 characters of
`thinking` on a `slugify` function** and still ran out before writing any code.

Per-token speed stops mattering when the token count explodes. Rung 0 exists for
cheap mechanical work; paying thousands of thinking tokens for boilerplate is
the wrong trade at any speed.

Spotting one before wasting a 25 GB download: the model card mentions reasoning
or thinking modes, a response carries a `thinking` field beside `content`, or a
short prompt returns `done_reason: "length"` with `content` empty. If you do
benchmark one, give it thousands of tokens — a small cap measures your budget
rather than the model, which is exactly how the first run here produced two
misleading empty answers.

Ladder itself handles the truncation correctly: `length` is a truncation stop
reason, so the job retries at the same rung with double the budget rather than
escalating to a paid tier. That mechanism works, but it cannot rescue a model
that needs 10k characters of thought per task.

## Tuning the policy

The whole cost policy is `TASK_RUNGS` in `ladder/tiers.py`:

```python
TASK_RUNGS = {
    "docstring": 0,   # free
    "review": 1,      # haiku
    "implement": 2,   # sonnet
    "debug": 3,       # sonnet, more thinking
    "architect": 4,   # opus
}
```

Two moves worth making after a week of real use:

**Push kinds down when the cheap tier is good enough.** If your local model
handles `test` acceptably, move it to rung 0 and stop paying for tests. Watch
the escalation rate in `ladder_stats` — if a kind almost never escalates, it is
starting too high.

**Push kinds up when they always escalate.** If `implement` escalates from
rung 2 to 3 most of the time, it is starting too low and you are paying for
the wasted rung-2 attempt every time. Start it at 3.

The escalation trail on every job records exactly which rungs were tried and
why each failed, so this tuning is driven by data rather than guesswork.

## Hard caps

Escalation ceilings are the safety rail:

- `max_rung: 0` — job is free or it fails. No API call is possible.
- `max_rung` equal to the starting rung — no escalation at all.
- Omitted — climbs to rung 5 if it has to.

For a swarm, `max_rung` applies to every task unless a task overrides it. A
1000-task swarm with `max_rung: 0` cannot cost anything, which makes it a safe
way to try a bulk operation before committing money to it.

## Reading the dashboard

The **saved by the ladder** figure compares actual spend against what the same
token volume would have cost entirely at rung 5. It is a ceiling comparison,
not a claim that you would otherwise have used Fable for everything — treat it
as a measure of how much work the cheap rungs absorbed, not as literal savings.
