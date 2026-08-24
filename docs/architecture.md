# Architecture

Small on purpose. Five modules, three engines, one table that encodes the whole
cost policy.

```
Claude Code ──stdio JSON-RPC──> mcp/ladder_mcp.py
                                       │
                                       ▼
                                  ladder.Router ──> engines ──┬─> Ollama    (rung 0, free)
                                       │                      ├─> Anthropic (rungs 1-5)
                                       │                      └─> claude -p (fallback)
                                       ▼
                                  ladder.Store (SQLite)
                                       ▲
                                       │
Browser ──HTTP──> ladder.server (Flask)
```

The MCP server and the Flask server both talk to the same SQLite database, and
neither depends on the other. Work driven from Claude Code appears in the
dashboard without the web server running; the dashboard works without Claude
Code attached. Either can be down without affecting the other.

## The pieces

### `ladder/tiers.py` — the policy

The only file most people need to edit. Defines the six rungs and, in
`TASK_RUNGS`, the starting rung for every task kind. Everything else reads from
here.

Each `Tier` carries the model id, price, concurrency budget, and the API
parameters that model accepts. That last part matters: Haiku 4.5 returns HTTP
400 if `output_config.effort` is present, so its tier declares `effort=None` and
the engine skips the parameter. Per-model API quirks live in the tier
definition rather than being scattered through call sites.

### `ladder/router.py` — the escalation loop

Runs one job. Starts at the resolved rung, and on failure moves up exactly one
rung, up to a ceiling. The linear climb is deliberate: jumping straight from
rung 0 to rung 5 on failure would find *an* answer but never the *cheapest*
one, and would make the audit trail useless.

A job fails when any of these is true:

- the engine raised or returned an error
- the output was empty
- the model returned `stop_reason: "refusal"` (which arrives as HTTP 200, so it
  must be checked explicitly before reading content)
- a caller-supplied verifier rejected the output

Verifiers are what make escalation more than decorative. `verify: "python"`
means the output must parse as Python; a local model that emits a truncated
function fails the check and the job climbs automatically. Without a verifier,
"success" only means the engine returned some text.

**Verifiers are structural, and that is a real limit.** They check that output
is well-formed, never that it is correct. Observed live: a 3B returned valid
JSON asserting `"charlie": 6` (it has 7 characters). The `json` verifier passed
it and the job was recorded as a rung-0 success carrying a wrong answer. This is
the most dangerous failure mode in the design, precisely because it is silent —
a crash would have been safer.

`adjudicate=True` closes the gap by asking the tier one rung up whether the
answer actually satisfies the task, escalating on rejection. Two deliberate
asymmetries in `Router._adjudicate`:

- The adjudicator is instructed to answer FAIL when unsure. A wrong answer that
  ships costs far more than one extra retry.
- A *broken* adjudicator passes by default. If the checker itself is failing,
  that is an infrastructure problem, and escalating every job would silently
  convert it into a spending problem.

The check reads an answer rather than producing one, so it costs a small
fraction of running the task at the higher rung. That asymmetry is what makes
"cheap tier does the work, dearer tier rules on it" the best value in the tool.

### `ladder/pool.py` — the swarm

Per-tier semaphores rather than one global limit, because the rungs scale
differently:

- **Rung 0 is CPU-bound and near zero-sum.** Local generation is limited by
  memory bandwidth. Eight concurrent local jobs do not finish eight times
  faster — each runs roughly eight times slower. Its cap is 2.
- **Rungs 1–5 are network-bound.** Fanning out is nearly free in wall-clock
  terms; the real limits are API rate limits and budget. Haiku's cap is 12.

Semaphores alone are not enough, and this is worth spelling out because the
first implementation got it wrong. With one shared thread pool, a thread that
blocks acquiring a saturated tier's semaphore **still occupies a pool slot**.
Submit 60 local tasks ahead of 3 Haiku tasks and the local ones claim every
thread; two run, the rest block holding threads hostage, and the Haiku tasks
never get scheduled. Measured: the first Haiku completion landed at position 29
of 63, despite Haiku having six times the concurrency budget.

So `Swarm.run` partitions tasks by starting rung and gives each rung its own
pool, sized to that rung's budget, with the rung pools running concurrently.
After the fix the first Haiku completion moved to position 2. The semaphores
are still there — they bound concurrency *across* simultaneous swarms, which a
per-swarm pool cannot see — but the partitioning is what guarantees fairness.

A slow, narrow tier can never hold a fast, wide one hostage. There is a
regression test for exactly this.

A task that raises is caught and recorded as a failed result; one bad task
never takes down a swarm.

### `ladder/store.py` — history

SQLite, two tables: `jobs` and `attempts`. Every attempt at every rung is
recorded with its model, tokens, cost, latency, and error.

This is the other half of keeping the orchestrator's context clean. The router
returns the final text plus accounting — never the transcript. When you want
the detail you open the dashboard or call `ladder_status`, rather than paying
context for output you may not read.

`Store.close()` exists because on Windows an open SQLite handle keeps a lock on
the file; the class supports the context-manager protocol for that reason.

### `ladder/engines/` — the backends

All three implement `run(tier, system, prompt, max_tokens) -> Result`. The
`Result` shape is uniform, so the router does not care which engine ran.

| Engine | Auth | Cost | Tools | Use for |
|---|---|---|---|---|
| `ollama` | none | free | no | rung 0, bulk mechanical work |
| `anthropic` | API key | rate card | no | rungs 1–5, volume |
| `cli` | Claude Code subscription | rate card **+ ~25–35k tokens/call** | yes | jobs that must edit files |

`OllamaEngine` uses only the standard library, so the free tier has no pip
dependencies at all.

`AnthropicEngine` encodes the current API contract: `effort` nested inside
`output_config`, adaptive thinking on models that support it, no
`temperature`/`top_p`/`top_k` (removed on these models — sending them is a 400),
no assistant prefill, streaming above 16k `max_tokens` so a long generation
cannot trip the HTTP timeout, and an explicit `refusal` check.

`ClaudeCliEngine` is the no-API-key fallback. See
[`cost-model.md`](cost-model.md) for why it is expensive and when it is still
the right choice.

Engine selection happens in `Router.engine_for`: rung 0 always goes to Ollama;
rungs 1–5 prefer the raw API and fall back to the CLI when no credential is
available.

### `mcp/ladder_mcp.py` — the MCP server

Hand-rolled JSON-RPC 2.0 over stdio, zero third-party dependencies. That is a
deliberate trade: the protocol surface needed here is `initialize`,
`tools/list`, and `tools/call`, which is about 60 lines, and avoiding an SDK
dependency means the server cannot break from an unrelated package upgrade.

Everything human-facing goes to **stderr**. stdout is the protocol channel and
one stray `print` there corrupts the session — `scripts/check_mcp.py` asserts
this in CI by failing on any non-JSON line.

The server calls the Router in-process rather than over HTTP, so the MCP tools
work whether or not the Flask server is running.

### `ladder/server.py` and `ladder/web/` — the dashboard

Flask REST API plus a single HTML file with no build step. Jobs run on a
background thread pool and the API returns a job id immediately, because a
rung-0 job can take a minute — far longer than any reasonable request timeout.

The dashboard polls every four seconds and shows live status, the escalation
trail per job, per-tier spend, and the comparison against running everything at
rung 5.

## Design decisions worth defending

**Why a linear climb rather than picking the right tier up front?** Predicting
which model a task needs is exactly as hard as the task. Trying the cheap one
and checking is cheaper than predicting, and the failure is informative — the
escalation trail tells you your policy is mistuned, which a predictor never
would.

**Why does the free tier use a 30B model?** Because on CPU it is no slower than
a 7B (see the benchmarks) and considerably more capable. Memory bandwidth, not
parameter count, sets the rate.

**Why SQLite rather than in-memory state?** Two independent processes need to
see the same jobs, history has to survive restarts, and the escalation trail is
the raw material for tuning the policy. A file-backed database is the simplest
thing that does all three.

**Why do the MCP tools return truncated results by default?** Because the
caller is a model with a finite context. `ladder_status` takes `full: true`
when you actually need the whole thing.

## Extending it

Adding a rung, an engine, or a task kind is documented in
[`CONTRIBUTING.md`](../CONTRIBUTING.md). The short version: rungs and kinds are
data in `tiers.py`; engines are a class with one method.
