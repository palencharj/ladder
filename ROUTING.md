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

Use `ladder_swarm` with `batch: true` for anything repetitive. The paid tiers
charge ~35k tokens of harness overhead **per invocation**, so ten batched tasks
cost that once instead of ten times.

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
ladder_run(prompt="...", kind="docstring")        # free, local
ladder_swarm(tasks=[...], kind="classify", batch=true)
ladder_review(paths=["a.py","b.py"])              # one job per file
```

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

The policy above is written to fire automatically on the work that fits and
stay silent on the work that does not. That is the whole design goal — the tool
should feel like it was already there, not like something you must remember.
