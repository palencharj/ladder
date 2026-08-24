# MCP tool reference

Seven tools. All are available in Claude Code once the server is registered.

Register at user scope (available in every project):

```bash
claude mcp add ladder --scope user -- python /path/to/ladder/mcp/ladder_mcp.py
```

Or rely on the checked-in `.mcp.json`, which Claude Code picks up automatically
when run from the repository directory.

---

## `ladder_health`

No parameters. Reports which engines are usable and — critically — which paid
path is actually in effect.

```
Engine health:
  OK    ollama: ollama 0.32.14
  DOWN  anthropic_api: no API credential found; set ANTHROPIC_API_KEY
  OK    claude_cli: claude CLI at C:\...\claude.EXE
  local models: qwen3-coder:30b, qwen2.5-coder:7b
  paid rungs (1-5) will use: cli
  NOTE: the CLI fallback adds ~25-35k tokens of harness overhead per call.
```

Call this first when anything behaves unexpectedly. The most common surprise —
rungs 1–5 costing far more than the rate card suggests — is visible here.

---

## `ladder_tiers`

No parameters. Prints the ladder and the default rung for every task kind.
Use it to check the current policy before overriding anything.

---

## `ladder_run`

Run one task on the cheapest tier that can do it.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `prompt` | string | *required* | The task. Self-contained — the worker sees no conversation history. |
| `kind` | string | `implement` | Sets the starting rung. See the kind table below. |
| `rung` | int | from `kind` | Force a starting rung, 0–5. |
| `tier` | string | — | Force a tier by name. Overrides `rung` and `kind`. |
| `model` | string | tier default | Swap the model at the starting rung only. `qwen2.5-coder:3b` classifies in ~1.7s vs ~34s for the 30B default. |
| `max_rung` | int | 5 | Escalation ceiling. Set equal to the start rung to forbid escalation. |
| `verify` | enum | — | `python`, `json`, or `nonempty`. Structural only — checks form, never correctness. Failure escalates one rung. |
| `adjudicate` | bool | `false` | Have the next rung up check the answer is *right*, and escalate if not. One small call; the only thing that catches semantically wrong output. |
| `system_extra` | string | — | Appended to the system prompt. Project conventions go here. |
| `max_tokens` | int | 8000 | Output cap. Use ~256 for classification. |
| `title` | string | prompt prefix | Label for the dashboard. |

Returns the final text plus a one-line accounting header. The full transcript
stays in the database.

```
Use ladder_run with kind "docstring" and verify "python" to document
the clamp() function in utils.py.
```

Starts free at rung 0. If the local model emits something that will not parse,
the verifier catches it and the job climbs to rung 1 on its own.

**Guaranteeing a job is free:** set `rung: 0, max_rung: 0`. No API call is
possible.

---

## `ladder_swarm`

Fan out many tasks concurrently, each on its own cheapest tier.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `tasks` | array | *required* | Each needs `prompt`; may override `kind`, `rung`, `tier`, `max_rung`, `verify`, `title`. |
| `kind` | string | `implement` | Default kind for tasks that do not set one. |
| `max_rung` | int | 5 | Default ceiling for all tasks. |
| `verify` | enum | — | Default verifier for all tasks. |
| `system_extra` | string | — | Shared context appended to every task's system prompt. |
| `wait` | bool | `false` | Block for all results, or return a `swarm_id` to poll. |

With `wait: false` you get a `swarm_id` immediately and a summary of the
planned tier distribution. Poll with `ladder_status`.

Per-tier concurrency caps are enforced automatically — a batch mixing rung 0
and rung 1 work runs the local jobs 2 at a time and the Haiku jobs 12 at a
time, in parallel with each other.

```
Use ladder_swarm to summarize each file under src/, kind "summarize",
max_rung 0.
```

Every task runs free, and the swarm cannot cost anything.

---

## `ladder_review`

Code review over a set of files, one job per file, concurrently.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `paths` | array | *required* | File paths to review. Unreadable paths are reported, not fatal. |
| `rung` | int | 1 (haiku) | Starting rung. `0` makes the whole pass free. |
| `max_rung` | int | = `rung` | Defaults to no escalation. |
| `focus` | string | — | Steer, e.g. "concurrency safety" or "error handling". |
| `wait` | bool | `true` | Block for results. |

Defaults to rung 1 because review needs fluency more than deep reasoning.
Raise it for security-sensitive code; drop to 0 for a free first pass.

The review prompt asks for defects justified by a concrete failure scenario and
explicitly discourages style nitpicking and invented findings.

---

## `ladder_status`

| Parameter | Type | Notes |
|---|---|---|
| `job_id` | string | One job, with its full escalation trail. |
| `swarm_id` | string | Every job in a swarm, with progress and cost. |
| `limit` | int | With neither id, list this many recent jobs. Default 20. |
| `full` | bool | Return complete result text instead of a 1200-character preview. |

The escalation trail is the useful part:

```
Escalation trail:
  attempt 1: rung 0 local (qwen3-coder:30b) FAIL 41200ms $0.00000 — failed python_parses check
  attempt 2: rung 1 haiku (claude-haiku-4-5) ok 2100ms $0.00042
```

That tells you the local model tried and failed, and what the fallback cost.
Repeated patterns like this are the signal to retune `TASK_RUNGS`.

---

## `ladder_stats`

No parameters. Spend summary, per-tier breakdown, and the comparison against
running the same token volume entirely at rung 5.

Treat the savings figure as a measure of how much work the cheap rungs
absorbed, not as literal money saved — it assumes the alternative was Fable 5
for everything.

---

## Task kinds

| Rung | Tier | Kinds |
|---|---|---|
| 0 | local (free) | `classify` `triage` `docstring` `boilerplate` `rename` `simple_edit` `summarize` `extract` |
| 1 | haiku | `doc` `readme` `test` `review` `commit_message` `changelog` |
| 2 | sonnet | `implement` `refactor` `migrate` |
| 3 | sonnet-high | `debug` |
| 4 | opus | `architect` |

An unrecognised kind falls back to `implement` (rung 2). Kinds are data — edit
`TASK_RUNGS` in `ladder/tiers.py` to retune.

---

## Patterns

**Free bulk pass, paid only where it fails.** Start everything at rung 0 with a
verifier and a ceiling of 1. Work the local model handles costs nothing; only
the failures reach Haiku.

```
ladder_swarm(tasks=[...], rung=0, max_rung=1, verify="python")
```

**Cheap work, checked by a smarter rung.** The highest-leverage pattern in the
tool. Local does the generation for free; Haiku only reads the answer and rules
on it, which is a fraction of the tokens of doing the task itself.

```
ladder_swarm(tasks=[...], rung=0, max_rung=1, verify="json", adjudicate=true)
```

Anything the local model gets right stays free. Anything it gets wrong is
caught and redone one rung up, rather than silently shipping.

**Budget-capped experiment.** Run a large swarm at `max_rung: 0` first. It
cannot cost anything, and the failure rate tells you how much of the batch
genuinely needs a paid tier before you spend on it.

**Project conventions without repeating yourself.** Put them in `system_extra`
once at the swarm level rather than in every prompt.

**Keeping your own context clean.** Use `wait: false`, then poll
`ladder_status` for a summary. Only pull `full: true` on the specific jobs
whose output you actually need.
