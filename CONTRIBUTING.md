# Contributing

## Setup

```bash
pip install -e ".[dev,api]"
pytest -q
python scripts/check_mcp.py
```

Both must pass before you push. CI runs them on Ubuntu and Windows across
Python 3.11–3.13.

## The one rule for tests

**Tests must not need a network, a model, or an API key.** The suite fakes
every engine so the escalation logic itself is what gets exercised. A test that
calls a real model is slow, flaky, costs money, and cannot run in CI.

If you genuinely need to exercise a live engine, that is a script under
`scripts/`, not a test.

## Common changes

### Retune the cost policy

Edit `TASK_RUNGS` in `ladder/tiers.py`. This is the change you will make most
often, and it is one line.

```python
TASK_RUNGS = {
    "test": 0,   # was 1 -- our local model handles tests fine
}
```

Drive it from data: `ladder_stats` shows escalation rates per tier. A kind that
almost never escalates is starting too high. A kind that almost always
escalates is starting too low and you are paying for a wasted attempt each time.

### Add a task kind

Two edits, both data:

1. `TASK_RUNGS` in `ladder/tiers.py` — the starting rung.
2. `KIND_PROMPTS` in `ladder/prompts.py` — the system prompt.

`test_every_task_kind_has_a_prompt` fails if you do the first and forget the
second.

Keep prompts short. At rung 0 every system-prompt token is prefill time on a
CPU managing roughly 20 tok/s, and long prompts are the exact overhead that
makes the CLI engine expensive.

### Add or change a rung

Add a `Tier` to `LADDER` in `ladder/tiers.py`. Rungs must stay contiguous from
zero and priced in non-decreasing order — two tests enforce that.

Encode the model's API constraints in the tier itself rather than in the
engine. If a model rejects `effort`, set `effort=None`; if it does not support
adaptive thinking, set `thinking=False`. Keeping quirks in the tier definition
is why `AnthropicEngine` has no per-model branching.

Set `concurrency` from how the tier scales, not from how much you want. Local
inference is bandwidth-bound and near zero-sum — more concurrency makes each
job slower, not the batch faster.

### Add an engine

Subclass `Engine` in `ladder/engines/` and implement:

```python
def available(self) -> tuple[bool, str]:      # (usable, human-readable reason)
def run(self, tier, system, prompt, max_tokens) -> Result
```

Requirements:

- **Never raise.** Return `Result(ok=False, error=...)`. The router treats a
  raised exception as a swarm-level failure; a returned error escalates
  properly.
- **Fill in the accounting.** `tokens_in`, `tokens_out`, `cost_usd`,
  `latency_ms`. The dashboard and the tuning workflow both depend on it.
- **Set `ok=False` on empty output.** A successful call that returned nothing
  is a failure, and should escalate.

Then wire it into `Router.engine_for` and export it from
`ladder/engines/__init__.py`.

### Add an MCP tool

1. Append a schema to `TOOLS` in `mcp/ladder_mcp.py`.
2. Write a `t_yourtool(args) -> {"text": ...}` handler.
3. Register it in `HANDLERS`.
4. Add the name to `EXPECTED_TOOLS` in `scripts/check_mcp.py`.

Write the `description` for a model, not a human. It is the only thing an
orchestrator sees when deciding whether to call your tool — say what it does,
when to reach for it, and what it costs.

**Never print to stdout from the MCP server.** stdout is the JSON-RPC channel;
one stray `print` corrupts the session. Use `log()`, which writes to stderr.
`check_mcp.py` fails CI on any non-JSON line, so this is caught rather than
debugged.

## Style

`ruff check ladder tests mcp` must be clean. Line length 100.

Comment the *why*, not the *what*. The valuable comments in this codebase
explain non-obvious constraints — why Haiku's `effort` is `None`, why rung 0's
concurrency is 2, why `Store` has a `close()`. Those exist because the
reasoning is not recoverable from the code.

## Reporting local performance

Benchmark numbers in the docs are from one machine with no discrete GPU, and
they are the weakest part of the documentation. If you run
`python scripts/bench.py` on different hardware — especially anything with a
GPU — a PR adding your numbers to `docs/cost-model.md` is genuinely useful.
Include the CPU, GPU, RAM speed, and Ollama version.
