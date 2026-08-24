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

`http://127.0.0.1:5151` — live jobs, escalation trails, per-tier spend, and a
running tally of what the ladder saved you versus sending everything to rung 5.

## Using it from Claude Code

Once registered, seven `ladder_*` tools are available.

**One task, cheapest tier that works:**

> Use ladder_run to add Google-style docstrings to `parse_config`, verifying the
> output parses as Python.

Kind `docstring` → starts at rung 0 → free. If the local model emits something
that will not parse, the `python` verifier catches it and the job climbs to
rung 1 automatically.

**Bulk work, fanned out:**

> Use ladder_swarm to write a one-line summary of every file under `src/`.

Per-tier concurrency caps apply automatically: local jobs run 2 at a time
because CPU inference is near zero-sum, while Haiku jobs fan out 12-wide.

**Code review as a default habit:**

> Use ladder_review on every file I changed, focused on error handling.

One job per file, concurrent, at rung 1 by default. Pass `rung: 0` to make an
entire review pass free.

**Pick a smaller local model for short answers:**

Rung 0 defaults to the 30B because for code generation it is no slower than a
7B and much smarter. For one-word answers that flips — a 3B classifies in
**1.7s** where the 30B takes **33.8s**, both free and both correct:

```
ladder_run(prompt="...", kind="classify", model="qwen2.5-coder:3b", max_rung=0)
```

The override applies to the starting rung only, keeping that rung's engine and
pricing. Any escalation above it uses each rung's standard model.

**Cap the spend on anything:**

Set `max_rung` equal to the starting rung and escalation is forbidden — the job
either succeeds cheaply or fails honestly. `max_rung: 0` guarantees a job never
costs a cent.

## The tools

| Tool | What it does |
|---|---|
| `ladder_health` | Which engines are usable, and which paid path is actually in effect |
| `ladder_tiers` | The ladder, plus the default rung for every task kind |
| `ladder_run` | One task on the cheapest sufficient tier |
| `ladder_swarm` | Many tasks concurrently, each on its own tier |
| `ladder_review` | Code review over a set of files, one job per file |
| `ladder_status` | Job or swarm state, with the full escalation trail |
| `ladder_stats` | Spend, per-tier breakdown, and savings versus rung 5 |

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

**Local speed is about your hardware, not the model.** Benchmarked on the
development machine (Intel Core Ultra 7 265U, no discrete GPU, DDR5-5600):
generation ran at **3–5 tokens/sec**, and a 7B model was no faster than a 30B
mixture-of-experts. Generation is bound by memory bandwidth and thermal
headroom, not parameter count — which is why the larger, smarter model is the
default. Run `python scripts/bench.py` to get your own numbers. Under about 10
tok/s, treat rung 0 as batch work you walk away from, not something a human
waits on. It is still free, which is the whole point.

**Without an API key, rungs 1–5 get expensive.** They then fall back to
shelling out to `claude -p`, which authenticates against a Claude Code
subscription but ships **~25–35k tokens of harness overhead on every single
call** — measured at ~$0.023 for a one-word reply. Stripping settings and MCP
config does not fix it; that only moved the overhead from ~35k to ~25k and
broke the prompt cache, making one call cost *more*. Set `ANTHROPIC_API_KEY`
and the same work goes over the raw API for a small fraction of that. Details
and measurements: [`docs/cost-model.md`](docs/cost-model.md).

`ladder_health` tells you which path is live at any moment.

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

- [`docs/architecture.md`](docs/architecture.md) — how the pieces fit, and why
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
