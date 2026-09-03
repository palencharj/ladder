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

### Default to Ladder, local rung first (per John, 2026-09-01)

Goal: convenience + lower token spend. Whenever a task fits the "send to
Ladder" profile below, route it there by default, starting at the local/free
rung (`max_rung: 0`), without waiting to be asked.

This does not override the "keep it in the main session" list below —
precision-critical, sequential, or interactive work still stays in-session,
because a wrong local-model result there costs more tokens to verify and redo
than it saves. The point is to stop *hesitating* on the clear cases, not to
force every case through Ladder.

### Routing work to Ladder

The `ladder_*` MCP tools run tasks on the cheapest model that can do them,
starting with a free local model. Prefer them for work that fits the profile
below. Route **by default** — do not wait to be asked.

**Send to Ladder when the work is:**

- **Mechanical and self-contained** — classification, triage, extraction,
  summarising, docstrings, boilerplate, renaming, changelog entries, commit
  messages.
- **Repetitive across many items** — the same operation over a list of files,
  functions, endpoints, or tickets. This is the strongest signal. Ten similar
  jobs is a swarm; one is not.
- **Latency-tolerant** — nobody is watching the cursor blink.

Use `ladder_spec` for anything repetitive. It is the cheapest path and should
be the reflex.

**What it does.** The free local model drafts every answer; ONE paid call
checks all the drafts at once; only the rejected ones are re-run, and those are
*corrected* from the draft rather than rewritten. Borrowed from speculative
decoding, and it works for the same reason: verification batches where
generation does not.

**Why not `ladder_swarm(batch: true)`.** Batching can only merge tasks that
share kind, verify, max_tokens and model, so mixed work fragments into one
invocation per bucket. Speculation does not bucket — every verification prompt
has the same shape. Measured on 8 real mixed tasks: **1 invocation against 6,
all 8 answers correct, ~39k tokens of allowance against ~210k.**

Batching is still right when the tasks really are uniform and you want the
answers written by the paid tier rather than merely checked by it.

**Keep it in the main session when the work is:**

- **Precision-critical** — a specific edit where being subtly wrong is worse
  than being slow. Verifying a local model's diff line by line costs more than
  writing it.
- **Sequential and interdependent** — each step informed by the last.
- **Reliant on whole-context judgement** — noticing what the tests *don't*
  cover, or that an error path is missing, requires one context holding the
  whole picture. Fan-out cannot do this, and pretending otherwise produces
  confident, shallow findings.
- **Interactive** — a person is waiting on the answer right now.

That second list is not a caveat, it is half the policy. Routing precision work
to a cheap tier produces work that has to be redone.

### How to call it

Let the task kind pick the tier. It is right most of the time:

```
ladder_spec(tasks=[{"prompt": "..."}, {"prompt": "..."}])   # <- the default
ladder_run(prompt="...", kind="docstring")                  # one item, free
ladder_route(prompts=["..."])                               # where would this go?
ladder_review(paths=["a.py","b.py"])                        # one job per file
```

`kind` is optional everywhere — it is inferred from the prompt text, so nobody
needs to learn the tier taxonomy to get work routed cheaply. `ladder_route`
answers "where would this go" for free, without calling any model.

Overrides worth knowing:

- `max_rung: 0` — guarantees a job cannot spend any allowance. Good for trying
  a large batch before committing to it.
- `adjudicate: true` — has the next rung up check the answer is *correct*, not
  merely well-formed. Use when a cheap tier's output has to be right. Costs one
  small extra call, far less than running the whole task a rung higher.
- `verify: "python" | "json"` — structural check only. It catches malformed
  output, never wrong output.
- `max_tokens` — raise it for long outputs. A truncated answer is retried with
  more budget at the same rung, but starting closer saves a round trip.

### Before a large batch

Warm the local model first. A cold 18 GB model costs ~33 s to page in against
0.3 s warm, and without this the first job of a batch pays that:

```
ladder_models(action="warm")
```

`ladder_models(action="status")` shows what is resident and whether the rung-0
model fits in available RAM. `action="unload_others"` frees everything else —
an idle model holds its full weight in RAM for nothing.

### After a batch

Call `ladder_report` occasionally. Two numbers matter:

- **Deflection rate per task kind.** A kind near 0% is starting too low and
  paying for a doomed local attempt every time — raise it in `TASK_RUNGS`. A
  kind at 100% may be starting too high.
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
