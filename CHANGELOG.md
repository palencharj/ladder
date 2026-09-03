# Changelog

All notable changes to Ladder.

This file was itself produced with Ladder: the nine commits were categorised by
the free local tier (9/9 correct, no allowance spent), and the prose below was
written by four tasks batched into a single `claude -p` invocation.

## [Unreleased]

### Added

- **Speculative execution** (`ladder_spec`). The free local model drafts every
  answer, ONE paid call verifies the whole batch, and only rejected drafts are
  re-run — corrected from the draft rather than rewritten. Borrowed from
  speculative decoding, where a small model proposes and a large one verifies
  in bulk. Measured on 8 real mixed tasks from this repo: **1 paid invocation
  against 6 for `ladder_swarm(batch=true)`**, all 8 answers correct, ~39k
  tokens of allowance against ~210k. The win is heterogeneity — batching can
  only merge tasks sharing kind, verify, max_tokens and model, while every
  verification prompt has the same shape.
- **Automatic task-kind inference** (`ladder/classify.py`, `ladder_route`).
  `kind` is now optional everywhere; it is read from the prompt text, so a
  caller needs no knowledge of the tier taxonomy. Judgement signals beat
  mechanical ones, because guessing cheap on an ambiguous prompt produces a
  confident useless answer.
- **Local generation share** — the fraction of generated output tokens that
  came off the free box, recorded per task kind alongside the acceptance rate.
  Deflection counts whole tasks and flatters a run where rung 0 answered ten
  trivial questions while the paid tier wrote the one long answer.
- **`speculations` table** recording every draft, verdict and correction. It is
  telemetry, and also a training corpus collected as a side effect of ordinary
  use: `Store.training_pairs()` returns rejected drafts paired with the answers
  that replaced them, which is the shape a draft-model fine-tune wants.
- **Context offload** (`ladder_recall`). Answers a question from the memory
  vault without loading the vault into the paid model's context. The local
  model selects passages; it never writes them. Every returned character is
  checked mechanically to appear verbatim in the file it cites, so fabrication
  is structurally impossible rather than merely discouraged. Falls back to the
  free search ranking when nothing verifies, so it is never worse than a
  keyword search. Measured: 3 verified excerpts, 1,033 chars, 250 s, zero
  allowance.
- Claude Code integration: a `ladder-bulk` subagent, a `/ladder` slash command,
  and a routing hook that now names the speculative path.

### Fixed

- **The CLI's output was decoded with the locale encoding.** `subprocess.run`
  with `text=True` and no `encoding=` uses `locale.getpreferredencoding()`,
  which is `cp1252` on a default Windows box, while the `claude` CLI emits
  UTF-8. Every em dash, curly quote and accented character in a *delivered
  answer* was silently corrupted — not just in logs. Found when a repaired
  documentation section came back containing mojibake.
- **`local_share` was measuring two different things and dividing them.** It
  compared Ollama's `eval_count` against the CLI's `output_tokens`, which
  carries harness overhead — a 1,047-character answer was reported as 1,486
  output tokens. It also counted verification output as paid authorship, though
  nobody receives a verdict. Redefined as the fraction of *delivered answer
  text* written by the free model, in characters on both sides. The real figure
  is **87%**, not the 4% first reported.
- Drafting swallowed exceptions. A future whose result is never requested hides
  its exception, so a task that raised vanished entirely — no draft, no
  rejection, no result. Now it fails visibly and goes to the repair pass.
- `_adjudicate` passed by default when the checker was unreachable. Correct for
  `run_job`, where a broken adjudicator should not escalate everything in
  flight; wrong for speculation, where the check is the only thing standing
  behind a rung-0 answer. It now takes `fail_open`, and speculation passes
  `False`.
- Repair results bypassed `run_job` and so were never written to the store —
  paid calls invisible to the dashboard.
- The store and the in-run counter disagreed on local share (100% vs 4%),
  because the verification call's own tokens were attributed to nothing. They
  are now split across the drafts that call judged.

## [0.1.0] — 2026-08-24

### Added

- Cheapest-tier-first orchestration with a six-rung ladder, from a free local
  Ollama model up to Fable 5
- MCP server exposing eight tools to Claude Code
- Flask dashboard and SQLite job store, sharing one database so work driven from
  either surface appears in both
- Adjudication layer that has the next rung up check whether a cheap tier's
  answer is actually correct, not merely well-formed
- Worth-it report producing a verdict — worth-it, marginal, or not-worth-it —
  from measured deflection rather than notional savings

### Fixed

- Free tier no longer times out into a paid one. A fixed 900s local deadline was
  shorter than the default token budget needs at CPU speeds, so free jobs timed
  out and the router escalated them to a paid rung
- Cross-tier starvation in the swarm executor, where one shared thread pool let
  blocked local tasks hold threads and starve faster tiers queued behind them

### Changed

- Reporting moved from dollars to subscription allowance, the unit that actually
  binds on a prepaid plan
- Added task batching, packing many tasks into one CLI invocation so the ~35k
  per-call harness overhead is paid once rather than per task
- Per-job model override at the starting rung, for swapping in a smaller, faster
  local model on short-output work

### Documentation

- Architecture guide covering the verifier/adjudicator split and why structural
  verification is not enough
- Troubleshooting for MCP scope conflicts and local inference speed
