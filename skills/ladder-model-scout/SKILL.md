---
name: ladder-model-scout
description: Find a better local model for Ladder's rung 0 on this specific machine. Use when the user asks whether a newer, better, faster, or bigger local model is available, whether their current model is still the right one, or wants to re-evaluate rung 0 after a hardware or workload change. Checks real available RAM, reads the user's own deflection data to learn what they actually run, searches the current Ollama library, and A/B tests candidates against the incumbent before recommending anything.
---

# Finding a better local model

Model recommendations age badly and generic benchmarks do not predict local
performance. This skill replaces both with measurement on the actual machine
against the actual workload.

**Never recommend a model without running step 5.** The whole point is that
this ends in a measured comparison, not a plausible-sounding suggestion.

## The rules that make this different from a web search

Three findings, each learned by getting it wrong first. They do most of the
filtering.

**Speed tracks _active_ parameters, not total size.** On a machine without a
discrete GPU, generation is memory-bandwidth-bound: what matters is how many
bytes cross the bus per token. Measured on one such laptop — a 3B dense model
at 13.3 tok/s, a 7B dense at 6.6, and a **30B mixture-of-experts at 11.5**,
because it activates only ~3B parameters. So a large MoE with few active
parameters is the sweet spot, and a dense model of the same footprint is far
slower. Prefer `NNb-aXb` naming (total-active) and read the active count.

**A model that does not fit in RAM is unusable, not slow.** Paging weights from
NVMe during generation is orders of magnitude off RAM speed, and worse for MoE,
where expert selection changes every token and turns it into random reads.
Leave ~8 GB of headroom for the OS and the user's actual work.

**Switching models is rarely the speed lever people expect.** Warm, a 3B and a
30B MoE answered the same prompt within 7% of each other. The 100× difference
was *residency*: ~33 s cold against 0.3 s warm. Check `LADDER_KEEP_ALIVE`
before concluding a model is slow.

**Reject reasoning models for rung 0, whatever their architecture says.** This
is the trap that active-parameter filtering does not catch. Tested here:
`nemotron-3.5-lightning:30b-a3b` matches the incumbent exactly on paper — 30B
total, 3B active, and per-token speed duly landed close (6.5 vs 10.7 tok/s).
It still lost 14x on wall clock, because it emits 10-50x more tokens to answer
the same question. It spent 9,705 characters of `thinking` on a slugify
function and ran out of budget before writing any code.

Per-token speed stops mattering when the token count explodes. Rung 0 is for
cheap mechanical work, and paying thousands of thinking tokens for boilerplate
is the wrong trade at any speed.

How to spot one: the model card mentions reasoning or thinking modes; a response
carries a `thinking` field beside `content`; or a short prompt returns
`done_reason: "length"` with `content` empty. Send `"hi"` with a tiny budget as
the very first call — a reasoning model emits thinking even for that.

**But do not reject it there. Try turning thinking off first.** Ollama accepts
`"think": false` in the `/api/chat` payload, and on a hybrid model that changes
everything. Measured 2026-09-03 on `qwen3.6:35b-a3b-coding`, same 8 tasks:

| | correct | total | thinking |
|---|---|---|---|
| default (thinking on) | 2/8 | 285.3 s | 9,362 chars |
| `think: false` | **8/8** | **35.7 s** | 0 |

Six of the eight failures were empty answers whose budget had been consumed by
reasoning. With thinking off the same model is accurate and 8x faster. So the
rule is: **a reasoning model is disqualified only if its thinking cannot be
disabled.**

Two things follow. Give any reasoning model a budget in the thousands when you
test it with thinking on, or you are measuring your token cap rather than the
model. And if a candidate only wins with `think: false`, remember that Ladder's
Ollama engine does not send that flag today — adopting such a model means a
code change, and a dropped flag silently returns rung 0 to the 2/8 result.
Count that fragility against it.

## Method

### 1. What does the machine actually have?

```
ladder_models(action="status")
```

Gives available RAM (the OS *available* figure, not *free* — Windows counts
reclaimable cache as in-use and understates it badly), what is installed, and
what is resident. Budget: `available + size_of_current_model - 8 GB headroom`.

### 2. What does the user actually run?

```
ladder_report()
```

The per-task-kind deflection table is the requirement spec. A kind at 100%
deflection is already handled — a better model cannot improve on it. A kind at
0% either starts above rung 0 by design (so the local model never saw it) or is
genuinely failing locally. **Only the second case is a reason to change model.**

If deflection is high across the board, say so and stop. The honest answer is
often "your current model is not the constraint."

### 3. Find current candidates

Ollama's library changes faster than any model's training data. Fetch it:

- `https://ollama.com/library?sort=newest` — what exists now
- `https://ollama.com/library/<name>/tags` — exact sizes per quantisation

Filter to: fits the budget from step 1, MoE with low active parameters, and
suited to the kinds from step 2. Note the context window if the user works with
large files.

Check more than one family. Qwen, Nemotron, Granite, Gemma, Mistral and the
gpt-oss line all ship MoE variants, and the best fit moves between releases.

### 4. Be honest about what does not fit

State rejected candidates and why in one line each. "80B/3B active, 256K
context, but 52 GB against a 45 GB budget" is more useful than silence, and
tells the user what a RAM upgrade would buy.

### 5. A/B against the incumbent — do not skip this

Pull the candidate, then run both over ~6 tasks drawn from the user's real
task kinds (step 2). Compare wall clock **and correctness**.

Correctness is the part that matters, and structural checks will not catch it.
A 3B once returned `{"charlie": 6}` — well-formed JSON, passes a `json`
verifier, and wrong; charlie has 7 letters. Include at least two tasks with a
verifiable right answer (a count, a precise summary of a small function) and
check the answers yourself rather than trusting a parse.

`python scripts/bench.py --models <candidate> <incumbent>` gives tok/s and
prefill separately. Warm both first — a cold model makes any comparison
meaningless.

### 6. Recommend, with the reasoning and the escape hatch

Switch globally:

```bash
setx LADDER_LOCAL_MODEL <model>
```

Needs a new terminal and a Claude Code restart, since the MCP server reads the
variable at startup. Per job, `model=` on `ladder_run` / `ladder_swarm` needs
neither.

If the incumbent wins or ties, **say so plainly and recommend no change**. A
tie means keep the model you have — a switch costs a download, RAM churn and
the risk of an unmeasured regression, and buys nothing.

## Reporting

Give the user: the RAM budget, what their deflection data implies, candidates
considered and rejected with reasons, the A/B numbers including any wrong
answers, and a recommendation with the command. Note anything you did **not**
test so they can judge how much weight the conclusion carries.
