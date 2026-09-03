# Ladder

Cheapest-tier-first agent orchestration. One MCP tool call, and the work lands
on the smallest model that can actually do it — starting with a **free local
model on your own machine** and climbing, one rung at a time, only when a job
genuinely fails.

Built for the case where you want to hammer a lot of small engineering tasks —
docstrings, triage, review passes, boilerplate, test scaffolding — without
thinking about API spend, and without stuffing a giant transcript into your
orchestrator's context.

```
rung  tier          engine     model              $/Mtok in   $/Mtok out
  0   local         ollama     qwen3-coder:30b        free         free
  1   haiku         anthropic  claude-haiku-4-5       $1.00        $5.00
  2   sonnet        anthropic  claude-sonnet-5        $3.00       $15.00
  3   sonnet-high   anthropic  claude-sonnet-5        $3.00       $15.00
  4   opus          anthropic  claude-opus-5          $5.00       $25.00
  5   fable         anthropic  claude-fable-5        $10.00       $50.00
```

## The idea

Most work in a coding session is not hard. Writing a docstring is not hard.
Deciding whether a bug report is a duplicate is not hard. Generating a test
stub is not hard. Paying frontier-model prices for that work is waste, and
routing it through your main session also burns the context you actually need
for the hard part.

Ladder makes the cheap path the default path:

1. **Every task kind has a starting rung.** `docstring` starts at rung 0 —
   free, local, on your CPU. `review` starts at rung 1. `debug` starts at
   rung 3. You can override, but you rarely need to.
2. **Escalation is linear and earned.** A job climbs exactly one rung when it
   fails, never more. "Fails" means the engine errored, the output was empty,
   the model refused, or a verifier you specified rejected the output — for
   example, generated Python that does not parse.
3. **Results come back small.** The caller gets the final text plus accounting.
   Prompts, intermediate attempts, and full transcripts stay in SQLite and are
   read in the dashboard, not pasted into your session.

The result: a job that a local model can handle costs nothing and never touches
your API bill. A job that needs Fable 5 still gets there — after proving the
four cheaper rungs could not do it.

## Quickstart

```powershell
git clone https://github.com/palencharj/ladder.git
cd ladder
.\scripts\setup.ps1
```

The setup script installs dependencies, pulls the rung-0 model, runs the tests,
verifies the MCP handshake, and registers the server with Claude Code.

On macOS or Linux:

```bash
pip install -e ".[dev,api]"
ollama pull qwen3-coder:30b
pytest -q && python scripts/check_mcp.py
claude mcp add ladder --scope user -- python "$(pwd)/mcp/ladder_mcp.py"
```

Then start the dashboard:

```bash
python -m ladder.server
```

`http://127.0.0.1:5151` — a worth-it verdict, live jobs, escalation trails, and
how much subscription allowance the local tier and batching have preserved.

**Restart Claude Code afterwards** so it picks up the MCP server.

## Using it from Claude Code

Once registered, eight `ladder_*` tools are available.

**One task, cheapest tier that works:**

> Use ladder_run to add Google-style docstrings to `parse_config`, verifying the
> output parses as Python.

Kind `docstring` → starts at rung 0 → free. If the local model emits something
that will not parse, the `python` verifier catches it and the job climbs to
rung 1 automatically.

**Bulk work, fanned out — and batched:**

> Use ladder_swarm with batch=true to write a one-line summary of every file
> under `src/`.

`batch: true` packs compatible tasks into a single `claude -p` invocation. Since
the ~35k harness overhead is charged per *call* rather than per task, this is the
biggest saving available on a subscription — measured at 175k tokens of
allowance across six tasks.

Per-tier concurrency caps apply automatically: local jobs run 2 at a time
because CPU inference is near zero-sum, while Haiku jobs fan out 12-wide.

**Code review as a default habit:**

> Use ladder_review on every file I changed, focused on error handling.

One job per file, concurrent, at rung 1 by default. Pass `rung: 0` to make an
entire review pass free.

**Choose a different local model when it earns its place:**

Warm, the 3B and the 30B are within 7% of each other (37.8 vs 35.4 tok/s), so
switching models is *not* the throughput lever it looks like. The 3B wins on
load time and footprint — 1.9 GB against 18 GB — which matters when RAM is
contended or the model will often be cold.

Per job:

```
ladder_run(prompt="...", kind="classify", model="qwen2.5-coder:3b", max_rung=0)
```

Or globally, for every session:

```bash
setx LADDER_LOCAL_MODEL qwen2.5-coder:3b
```

The per-job override applies to the starting rung only, keeping that rung's
engine and pricing. Any escalation above it uses each rung's standard model.

**Make a cheap tier trustworthy:**

`verify` checks *form*, not *correctness*. A 3B will happily return well-formed
JSON claiming `"charlie"` has 6 characters, and `verify: "json"` passes it —
observed live. Structural verifiers cannot catch a wrong answer.

`adjudicate: true` asks the **next rung up** whether the answer is actually
right, and escalates if it is not:

```
ladder_run(prompt="...", kind="extract", rung=0, verify="json", adjudicate=true)
```

Tested against that exact failure: the adjudicator rejected `charlie: 6` with
"it has 7 characters (c-h-a-r-l-i-e)", approved the correct answer, and caught
an answer that silently omitted a key. One small check call costs far less than
running the whole task a rung higher — this is how you trust the free tier on
work that has to be right.

**Cap the spend on anything:**

Set `max_rung` equal to the starting rung and escalation is forbidden — the job
either succeeds cheaply or fails honestly. `max_rung: 0` guarantees a job never
costs a cent.

## Speculative execution — the cheapest path

The free local model drafts **every** answer, then **one** paid call checks all
the drafts at once, and only the rejected ones are re-run. It is speculative
decoding's trick applied to a different bottleneck: there, a big model verifies
K tokens in one forward pass; here, one `claude -p` call verifies K answers for
the same ~35k of harness overhead a single answer would cost.

```
ladder_spec(tasks=[{"prompt": "..."}, {"prompt": "..."}])
```

Measured on 8 real mixed tasks from this repository:

| path | paid invocations | est. allowance | wall | correct |
|---|---|---|---|---|
| **speculative** | **1** | **~39k** | 74.9 s | 8/8 |
| `ladder_swarm(batch=true)` | 6 | ~210k | 38.1 s | 8/8 |
| unbatched | 8 | ~280k | — | — |

**The competitor is batching, not naive calls.** For uniform tasks
`batch=true` is already one invocation. What speculation beats is *mixed* work:
batching can only merge tasks sharing kind, verify, max_tokens and model, so
eight mixed tasks fragmented into six buckets. Every verification prompt has
the same shape, so speculation does not bucket at all.

It costs about **2× the wall clock** — drafting comes first and local
generation is slow. You are buying allowance, not speed.

Do not speculate on judgement work. Asked to review a file for correctness
bugs at rung 0, the local model returned a summary with emoji headings, found
none of the two real bugs, and stated two false things about the code. The
verifier catches that — but a run where every draft loses costs more than going
straight to the paid tier. Watch the per-kind acceptance rate in
`ladder_report`.

Full detail, including where the analogy breaks: [docs/speculative.md](docs/speculative.md).

### Nobody has to pick a rung

`kind` is optional everywhere. It is inferred from the prompt text, with
judgement signals beating mechanical ones — "write a docstring explaining why
this bug happens" is debugging, not a docstring, and guessing cheap there
produces a confident useless answer.

```
ladder_route(prompts=["Add docstrings across the package"])
```

Free, calls no model, and tells you exactly where something would land.

### It builds its own training data

Every speculation records the task, the draft, the verdict and the final
answer. Accepted rows are examples of work the local model already does;
rejected rows pair a bad answer with its correction. `Store.training_pairs()`
returns the latter in the shape a fine-tune wants. Fine-tuning the draft model
is the standard way to raise the acceptance rate, and the acceptance rate is
the single number deciding how much work stays free — so using the tool builds
the dataset that makes the tool better.

## The tools

| Tool | What it does |
|---|---|
| `ladder_health` | Which engines are usable, and which paid path is actually in effect |
| `ladder_tiers` | The ladder, plus the default rung for every task kind |
| `ladder_run` | One task on the cheapest sufficient tier |
| `ladder_swarm` | Many tasks concurrently, each on its own tier |
| `ladder_review` | Code review over a set of files, one job per file |
| `ladder_status` | Job or swarm state, with the full escalation trail |
| `ladder_stats` | Spend and per-tier breakdown |
| `ladder_models` | Manage local models: what is installed, resident, and whether it fits in RAM. Warm before a batch. |
| `ladder_report` | **Is this tool worth running?** A verdict, the numbers behind it, and ranked actions — per user and per task kind |

Full parameter reference: [`docs/mcp-tools.md`](docs/mcp-tools.md).

## Task kinds

The kind sets the starting rung. This table *is* the cost policy.

| Rung | Tier | Kinds |
|---|---|---|
| 0 | local (free) | `classify` `triage` `docstring` `boilerplate` `rename` `simple_edit` `summarize` `extract` |
| 1 | haiku | `doc` `readme` `test` `review` `commit_message` `changelog` |
| 2 | sonnet | `implement` `refactor` `migrate` |
| 3 | sonnet-high | `debug` |
| 4 | opus | `architect` |

Retuning is a one-line edit to `TASK_RUNGS` in
[`ladder/tiers.py`](ladder/tiers.py). If your local model handles `test` fine,
move it to rung 0 and stop paying for tests.

## Two things worth knowing before you rely on this

**Local speed is about bandwidth and residency, not model size.** Benchmarked
on the development machine (Intel Core Ultra 7 265U, no discrete GPU,
DDR5-5600): the 30B mixture-of-experts generates at **11.5 tok/s**, nearly
double the 6.6 tok/s of a 7B dense model, because MoE activates only ~3B
parameters per token. Speed tracks *active* parameters — the signature of a
memory-bandwidth limit rather than a compute one. That is also why an NPU does
not help here: it shares the same system RAM.

The bigger lever is keeping the model **resident**. Cold, the 30B costs ~33 s
to page in; warm, the same request takes 0.3 s. Ladder sends `keep_alive`
(30 minutes by default, `LADDER_KEEP_ALIVE` to change).

Sustained runs throttle: 11.5 tok/s cool falls to ~3 tok/s after minutes of
continuous inference on a 15 W part. Run `python scripts/bench.py` for your own
numbers.

**Every `claude -p` call costs ~35k tokens before it does any work.** That is
the Claude Code harness — system prompt plus tool definitions — reloaded on each
invocation, measured on this machine. Stripping settings and MCP config does not
fix it; that only moved it from ~35k to ~25k and broke the prompt cache, making
one call cost *more*.

On a prepaid plan that overhead is **subscription allowance, not money**, which
makes it sharper: it is charged per invocation however trivial the task, so a
hundred one-line jobs burn ~3.5M tokens of quota on overhead alone. Two levers
follow, and they are the whole point of this tool:

- **Deflect** work to rung 0, where it spends no allowance at all.
- **Batch** what cannot be deflected. Measured: six classifications in **one**
  invocation, 175k tokens of allowance saved versus six separate calls.

`ladder_report` measures both. Details: [`docs/cost-model.md`](docs/cost-model.md).

`ladder_health` tells you which path is live at any moment.

## Making it the default

Ladder is most useful when nobody has to remember it exists. Add one line to
your `~/.claude/CLAUDE.md`:

```
@C:/path/to/ladder/ROUTING.md
```

That imports a routing policy telling Claude Code to send mechanical, repetitive,
latency-tolerant work through the `ladder_*` tools automatically — and, just as
importantly, to keep precision and interactive work in the main session where it
belongs. See [`ROUTING.md`](ROUTING.md) for the policy and the reasoning.

Update the policy in one place and everyone who imported it picks up the change.

## Two deeper integrations

The `@import` in `CLAUDE.md` is loaded once per session and then competes with
everything else in context. Two optional pieces push harder.

### A routing hook that runs on every prompt

`scripts/route_hint.py` is a `UserPromptSubmit` hook. It sees each prompt, and
injects guidance **only when the prompt is actually routable** — so the reminder
arrives at the moment of the decision rather than scrolling away hours earlier.

```json
"UserPromptSubmit": [
  { "hooks": [ { "type": "command",
    "command": "python \"/path/to/ladder/scripts/route_hint.py\"", "timeout": 10 } ] }
]
```

It stays silent on ordinary prompts, and deliberately stays silent on precision
work even when that work *also* looks bulk — "debug why every test fails" is
still debugging, and fanning it out produces confident shallow findings.

It also catches the expensive silent failure: **if Ollama is down, it says so**.
Otherwise every job falls through to a paid rung and nothing in the UI tells you.

A hook cannot force a tool call — nothing can. It makes the cheapest correct
option impossible to overlook.

### A skill that finds a better local model

`skills/ladder-model-scout/` — copy it to `~/.claude/skills/` and ask *"is there
a better local model for me?"*

It checks real available RAM, reads your own deflection data to learn what you
actually run, searches the current Ollama library, and **A/B tests candidates
against your incumbent before recommending anything**. It is built to conclude
"keep what you have," which is the usual right answer.

Three rules encoded in it, each learned by getting it wrong first:

- **Speed tracks _active_ parameters, not total size.** Measured here: 3B dense
  13.3 tok/s, 7B dense 6.6, 30B MoE **11.5** — the MoE nearly matches the 3B
  because it activates ~3B parameters. A large MoE is the sweet spot.
- **A model that does not fit in RAM is unusable, not slow.** Paging weights
  from NVMe is orders of magnitude off RAM speed, and worse for MoE.
- **Switching models is rarely the speed lever people expect.** Warm, a 3B and a
  30B MoE were within 7%. The 100x difference was residency — 33s cold against
  0.3s warm. Check `LADDER_KEEP_ALIVE` before blaming the model.

## Troubleshooting

**"Server 'ladder' is defined in multiple scopes."** Expected if you both
cloned the repo (which ships a project-scope `.mcp.json`) and ran
`claude mcp add --scope user`. Harmless here — the warning is about OAuth
token storage, and this is a local stdio server with no authentication. The
user-scope entry wins. To silence it, drop whichever you do not want:

```bash
claude mcp remove ladder -s project   # keep the global registration
```

Register at user scope if you want the tools in every project; rely on the
checked-in `.mcp.json` if you only want them when working in this repo.

**You edited the MCP server and nothing changed.** The server is a
long-running process started when Claude Code connected, so it keeps running
the code it was launched with. Restart Claude Code to pick up edits to anything
under `ladder/` or `mcp/`.

This bites hardest when you are testing a fix through the tool itself: the call
succeeds, the old behaviour persists, and it looks like the fix did not work.
`python scripts/check_mcp.py` spawns a fresh server, so use that to check a
change before concluding anything.

**A call succeeded but ran on the wrong tier.** Since v0.1.1 unknown arguments
are rejected with a message naming what is accepted. Before that they were
silently dropped — a `ladder_swarm` call passing a top-level `rung` reported
success while running every task on its kind's default rung instead. If you are
on an older build, check the `by_kind` deflection column in `ladder_report`
against what you expected.

**Rungs 1–5 cost far more than the rate card.** You have no
`ANTHROPIC_API_KEY`, so they are falling back to the `claude -p` CLI. Run
`ladder_health` to confirm, and see [`docs/cost-model.md`](docs/cost-model.md).

**Rung 0 jobs fail or hang.** Check Ollama is up (`ollama list`) and that the
rung-0 model is pulled. A cold model load adds tens of seconds to the first
call. `ladder_health` lists the models it can see.

**Everything local is unbearably slow.** Run `python scripts/bench.py`. If
generation is under ~10 tok/s you are on CPU inference — that is expected and
not a bug. Use rung 0 for batch work, not for anything interactive.

## Documentation

- [docs/speculative.md](docs/speculative.md) — speculative execution: the analogy, where it breaks, and what it measured
- [`ROUTING.md`](ROUTING.md) — make Ladder the default path, and what not to route
- [`docs/architecture.md`](docs/architecture.md) — how the pieces fit, and why
- [`docs/npu.md`](docs/npu.md) — why the Intel NPU is measured, and not used
- [`skills/ladder-model-scout/`](skills/ladder-model-scout/SKILL.md) — a skill that finds a better local model for your machine
- [`docs/cost-model.md`](docs/cost-model.md) — measured costs, the CLI overhead trap, tuning
- [`docs/mcp-tools.md`](docs/mcp-tools.md) — full tool and parameter reference
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — adding a rung, an engine, or a task kind

## Layout

```
ladder/
  tiers.py      the ladder and the task-kind policy  <- start here
  router.py     escalation loop and verifiers
  pool.py       swarm executor with per-tier concurrency caps
  store.py      SQLite job and attempt history
  server.py     Flask REST API
  engines/      ollama (free) | anthropic (API) | cli (subscription fallback)
  web/          dashboard, single file, no build step
mcp/            MCP stdio server, zero dependencies
scripts/        setup, benchmark, CI protocol check
tests/          37 tests; no network, no models, no API key
```

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) for rung 0 (optional — without it, rung 0 is unavailable and everything costs money)
- An `ANTHROPIC_API_KEY` for cheap rungs 1–5 (optional — without it, the `claude` CLI is used at much higher cost per call)

## Licence

MIT
