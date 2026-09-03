---
description: Show what Ladder is saving, or route work through it
---

# /ladder

Arguments given: `$ARGUMENTS`

## With no arguments

Report the state of the system, in this order:

1. `ladder_health` — is the local model reachable, and which paid path is in
   effect.
2. `ladder_models(action="status")` — what is resident, and whether the rung-0
   model fits in available RAM.
3. `ladder_report` — the verdict, the deflection rate, and the speculation
   block (acceptance rate and local generation share).

Then say, in two or three sentences, whether the tool is currently earning its
keep. If the report says `not-worth-it`, believe it and say so plainly rather
than looking for a flattering reading of the numbers.

## With arguments

The arguments describe work to route. Decide where it goes and be explicit
about the reasoning:

- **Many similar items** (docstrings across a package, a backlog to classify,
  a summary per function) → build the task list and call `ladder_spec` **once**
  with all of them. Warm the model first with `ladder_models(action="warm")` if
  there are more than a couple. The saving comes from one verification call
  covering every draft, so one call with twelve tasks is right and twelve calls
  are wrong.
- **One mechanical item** → `ladder_run`. Speculation needs a list to amortise
  against.
- **Precision, sequential, or judgement work** → keep it in this session and
  say why. Routing it to a cheap tier produces work that has to be redone,
  which costs more than it saved.

If you are unsure where something belongs, `ladder_route` will tell you what
kind and rung a prompt infers to, without running anything or spending
anything.

## Reporting back

Give the acceptance rate, the number of paid invocations against what the work
would have cost unbatched, and any drafts that were rejected. If acceptance was
low, say the run probably was not worth it — the token figure alone flatters a
run where every draft lost and the verify call was wasted.
