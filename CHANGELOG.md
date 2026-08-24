# Changelog

All notable changes to Ladder.

This file was itself produced with Ladder: the nine commits were categorised by
the free local tier (9/9 correct, no allowance spent), and the prose below was
written by four tasks batched into a single `claude -p` invocation.

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
