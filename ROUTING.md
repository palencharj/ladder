# Routing policy — making Ladder the default path

Drop this into your Claude Code instructions and mechanical work starts going
through Ladder automatically, without anyone having to remember it exists.

## Install it

**For every project you work on** (recommended), add one line to
`~/.claude/CLAUDE.md` — on Windows, `C:\Users\<you>\.claude\CLAUDE.md`:

```
@C:/Users/<you>/path/to/ladder/ROUTING.md
```

The `@` import pulls this file in wherever you are. Update it here and everyone
who imported it gets the change — no need to re-paste anything.

**For one repository only**, add the same line to that repo's `CLAUDE.md`.

**For the team**, have everyone clone the repo and add the import line. Or paste
the policy block below directly into a shared `CLAUDE.md` if you would rather
not depend on a local path.

---

## The policy block

<!-- everything below this line is the instruction Claude reads -->

### No local tier (per John, 2026-09-22)

Ladder's rung 0 (Ollama) is **disabled**. Every Ladder job now runs on a paid
Anthropic rung, rung 1 (Haiku) and up. Do not start or install Ollama, and do
not ask for rung 0: `rung: 0` or `max_rung: 0` is refused with an explicit
error rather than silently billed on a paid rung. `ladder_spec` (local drafts,
one paid check) and `ladder_models` (Ollama model management) are refused for
the same reason. `LADDER_ENABLE_LOCAL=1` restores the old behaviour.

### Routing work to Ladder

With no free tier, Ladder's remaining value is **amortising the ~35k
per-invocation overhead** of `claude -p` across many uniform items. That is a
narrower case than before, so route to it only when it clearly fits.

**Send to Ladder when the work is:**

- **Repetitive across many uniform items** — the same mechanical operation
  over a list of files, functions, endpoints, or tickets. Ten similar jobs is a
  batch; one is not. Use `ladder_swarm(batch: true)`: it answers many
  same-shaped tasks in one invocation instead of one each.
- **Latency-tolerant** — nobody is watching the cursor blink.

A single mechanical item is not worth routing any more: it costs a whole paid
invocation either way.

**Keep it in the main session when the work is:**

- **Precision-critical** — a specific edit where being subtly wrong is worse
  than being slow.
- **Sequential and interdependent** — each step informed by the last.
- **Reliant on whole-context judgement** — noticing what the tests *don't*
  cover, or that an error path is missing, requires one context holding the
  whole picture. Fan-out cannot do this, and pretending otherwise produces
  confident, shallow findings.
- **Interactive** — a person is waiting on the answer right now.

That second list is not a caveat, it is half the policy.

### How to call it

```
ladder_swarm(tasks=[{"prompt": "..."}, ...], batch=true)   # <- the default
ladder_route(prompts=["..."])                              # where would this go? (no model call)
ladder_review(paths=["a.py","b.py"])                       # one job per file
```

`kind` is optional everywhere — it is inferred from the prompt text.

Overrides worth knowing:

- `max_rung` — escalation ceiling. It must be 1 or above; `max_rung: 0` is an
  error now, because there is no free rung for it to mean.
- `adjudicate: true` — has the next rung up check the answer is *correct*, not
  merely well-formed.
- `verify: "python" | "json"` — structural check only. It catches malformed
  output, never wrong output.
- `max_tokens` — raise it for long outputs.

### After a batch

Call `ladder_report` occasionally. Two numbers matter:

- **First-try rate per task kind.** A kind that rarely passes at its
  starting rung is starting too low — raise it in `TASK_RUNGS`.
- **Unbatched paid tasks.** Each is its own ~35k invocation. If that climbs,
  pass `batch: true`.

The report will say `not-worth-it` when the tool is not earning its keep.
Believe it.

<!-- end of policy block -->

---

## Why not route *everything*

Because it would make things worse, and there is direct evidence.

A parallel session working on a C# codebase declined to use Ladder and gave
sound reasons: the work was precise edits and reasoning about safety
invariants, sequential rather than fan-out shaped, and its best findings came
from noticing what the tests did not cover — which needs one context holding
the whole picture, not ten workers holding fragments.

It was right. Ladder's value is real but bounded: it deflects **bulk mechanical
work** off the paid path. Sending precision work through it produces output you
have to re-verify line by line, which costs more than doing it inline.

Measured since, and worth knowing before trusting rung 0 with judgement: asked
to review a 500-line file for correctness bugs, the local model returned a
summary with emoji headings, found **none** of the two real bugs a careful read
found, and stated two false things about the code. Speculation's verifier would
have rejected that draft — but a run where every draft is rejected costs *more*
than going straight to the paid tier. Check the per-kind acceptance rate in
`ladder_report` and stop speculating on kinds that sit near zero.

The policy above is written to fire automatically on the work that fits and
stay silent on the work that does not. That is the whole design goal — the tool
should feel like it was already there, not like something you must remember.
