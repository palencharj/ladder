# Speculative execution

Draft everything on the free local model, have one paid call check all the
drafts at once, and re-run only what fails. The idea is lifted from
speculative decoding in LLM inference; this page explains how far the analogy
carries, where it stops, and what it actually measured.

## The original, briefly

A small **draft** model generates K tokens one at a time. The large **target**
model then verifies all K in a *single forward pass*, because attention
parallelises over positions — scoring K tokens costs one pass where generating
them costs K. The longest valid prefix is kept; the rest is thrown away.

The load-bearing property is not that the draft model is small. It is that
**verification batches and generation does not.**

## Why it transplants

Ladder has the same asymmetry for a completely different reason. A `claude -p`
invocation spends ~35k tokens of harness overhead before it does any work, and
that cost is per *invocation*, not per task. One call that checks twenty
answers costs what one call checking a single answer costs.

So the expensive thing — invoking the paid tier — happens once for the whole
batch, exactly as the expensive forward pass does in the original.

## Where it is better than the original

In real speculative decoding, rejecting token *i* invalidates every token
after it: they were generated conditioned on a token that turned out wrong.
Expected accepted length is

    (1 − α^(K+1)) / (1 − α)

which **saturates**. Even at α = 0.9, raising K past ~20 buys almost nothing.

Independent tasks have no causal chain. Task 4 was not conditioned on task 3,
so rejecting task 3 leaves it alone. Acceptance is per-item and expected yield
is **K·α — linear in K, no saturation.** Bigger batches keep paying, which is
precisely the regime a fixed 35k per-invocation cost rewards.

## Where it is worse, stated plainly

**It is not lossless.** The original is provably distribution-identical to
running the target alone, because rejection sampling has the target's real
logits. Here the verifier is a *judge*. "The target model thinks this answer
looks correct" is not "the target model would have produced this answer". This
is speculative decoding's economics with a quality gate where the sampler
should be. The guarantee is statistical, not exact.

**Verification is not free in K.** A transformer verifies K tokens in one pass
because positions are parallel. Our verify prompt has to physically *contain*
the K drafts, so its cost grows with total draft length. Hence `CHUNK_CHARS`.

**It is slower.** Drafting happens before verification and local generation is
slow. Measured below: about 2× the wall clock of running the work paid. This
buys allowance, not speed.

## What it measured

Eight real tasks from this repository — docstring, two classifications, two
summaries, an extraction, a dataclass, a commit subject — run three ways.

| path | paid invocations | est. allowance | wall | correct |
|---|---|---|---|---|
| **speculative** | **1** | **~39k** | 74.9 s | 8/8 |
| `ladder_swarm(batch=true)` | 6 | ~210k | 38.1 s | 8/8 |
| unbatched | 8 | ~280k | — | — |

**The competitor is batching, not naive calls.** For a homogeneous batch,
`batch=true` is already one invocation and speculation cannot beat it. What
speculation beats is **heterogeneity**: `_batch_key` splits a batch on kind,
verify, max_tokens, model and rung, so mixed work fragments into buckets — six
of them here. Speculation does not bucket, because every verification prompt
has the same shape. It **homogenises work that batching cannot merge**.

It also removes the shared-output-budget failure. `run_batch` clamps
`max_tokens × N` to 32k; overrun loses the entire batch. Verdicts need ~200
tokens each, so that ceiling stops mattering.

### How much of the writing stays local

**87% of delivered answer text**, across 14 drafts at 93% acceptance.

That number was wrong twice before it was right, and the corrections are worth
recording because both were measurement faults rather than system faults.

The first version compared Ollama's `eval_count` — real generated tokens —
against the CLI's `output_tokens`, which carries harness overhead: a
1,047-character answer came back reported as **1,486 output tokens**, roughly
5× the visible text. Two engines counting different things, divided by each
other. It reported 4%.

The second fault was conceptual. Verification output was counted as paid
*authorship*, but nobody receives a verdict — it is overhead, and it already
appears in the allowance figure. Counting it twice punished the local model for
being checked.

So the metric is now: **the fraction of delivered answer text written by the
free model, measured in characters on both sides.** An accepted draft was
written locally; a rejected one was rewritten by the paid tier. Verdicts count
in neither.

Per kind, from real runs:

| kind | drafts | accepted | local share of delivered text |
|---|---|---|---|
| doc | 3 | 67% | 68% |
| summarize | 2 | 100% | 100% |
| classify | 2 | 100% | 100% |
| (others, 1 each) | 6 | 100% | 100% |

The single rejected draft cost 1,047 characters of paid rewriting against
2,235 written locally — even the failure case leaves most of the writing on the
free box.

## Where it does not belong

Measured, not assumed. Asked to review `speculate.py` for correctness bugs at
rung 0, the local model returned a cheerful summary with emoji headings, found
**none** of the two real bugs a careful read found, and asserted two false
things about the code. Free, and worth nothing.

Do not speculate on:

- **Judgement work** — review, architecture, "is this a good idea".
- **Sequential work** — where step 3 depends on step 2's output.
- **A single task** — nothing to amortise; the verify call is pure overhead.

The verifier does catch these — that bad review would have been rejected — but
a run where every draft loses costs *more* than going straight to the paid
tier. Watch the acceptance rate per kind in `ladder_report` and stop
speculating on kinds that sit near zero.

## The verifier grades what it can check

The sharpest limit in practice, found on the first real run and worth more
attention than the token arithmetic.

A draft documenting `ladder_route` was **accepted** by the rung-1 verifier. It
contained a command-line syntax that does not exist, and an example output with
invented field values — `rung: basic_qa`, `rung: advanced_tutorial`,
`task_kind: information_request`. None of those are real; the rungs are 0–5 and
named `local`, `haiku`, `sonnet`. The example prompts were about the capital of
France.

The adjudicator is already told to "verify any arithmetic, counting, or factual
claim yourself rather than assuming it is right" and to "answer FAIL if unsure".
It passed the draft anyway — not from carelessness, but because **it had no
ground truth to check against.** Handed a task and an answer and nothing else,
a judge can confirm that the section has the right heading, a table with the
right columns, and an example in the right place. It cannot know that the
example is fiction.

So: **accepted means checked, not correct.** The guarantee is only ever as
strong as what the verifier was given.

### The experiment

The natural comparison was already inside that batch. Two tasks in it asked for
tool reference documentation. For one, the prompt listed all seven parameters
and their defaults. For the other, the prompt said *"include a plausible example
of the returned lines"* — asking for output that was never specified.

The first came back **completely accurate**. The second invented everything.
The local model did exactly what it was told in both cases; the difference was
entirely in the prompt.

So the same six tasks were re-run with real ground truth pasted in — actual
parameter lists, the real rung names, and a verbatim transcript of real output
instead of a request for a plausible one. Same task set, same `verify_rung`, one
variable changed.

| | control (no ground truth) | treatment (ground truth) |
|---|---|---|
| tasks | 6 | 6 |
| accepted | 5 (**83%**) | 6 (**100%**) |
| paid invocations | 2 | **1** |
| fabricated content | **1 draft** | **0** |
| delivered text written locally | 86% | **100%** |

Every invented token from the control run was gone, and the real transcript was
reproduced verbatim. All six drafts were then audited against the real kind
names, rung names and parameter list; none contained an invented one.

Note what the acceptance rate alone would have hidden. The control run scored
83% — respectable — while silently shipping a fabricated command-line syntax.
**Acceptance measures whether the verifier objected, not whether the answer is
true**, and those come apart precisely when the verifier has nothing to check
against.

### The rule

1. **Put the ground truth in the prompt.** Paste the real signature, schema, or
   output. It fixes both ends at once: the local model stops inventing, and the
   verdict becomes factual rather than structural.
2. **Never ask for a "plausible" or "example" anything** you have not specified.
   That is a request to fabricate, and it will be granted.
3. **Do not speculate on facts the verifier cannot reach.** Where ground truth
   cannot be supplied, the work is not a speculation candidate however
   mechanical the writing looks.

The corollary, stated plainly: skim accepted output when it makes factual
claims. Speculation buys you the volume of work, not the last mile of review.

## The flywheel

Every speculation writes a row to the `speculations` table: the task, the
draft, the verdict, and the answer finally returned. That is telemetry, and it
is also a training corpus collected as a side effect of ordinary use:

- **Accepted** rows are positive examples — work the local model already does.
- **Rejected** rows pair a bad answer with its correction.

`Store.training_pairs()` returns the second set in the shape a fine-tune wants.
Fine-tuning the draft model on a team's own codebase is the standard way to
raise α in speculative decoding, and α is the single number deciding how much
work stays free. No labelling pass, no separate data-gathering project — using
the tool builds the dataset.

## Using it

```
ladder_spec(tasks=[{"prompt": "..."}, {"prompt": "..."}])
```

`kind` is optional; it is inferred from the prompt text, so a caller needs no
knowledge of the tier taxonomy. `ladder_route(prompts=[...])` shows where
something would go without running it.

**One caveat found in use.** Inference reads the *whole* prompt, quoted
examples included. A task asking for documentation *about* debugging — one
containing the sentence "write a docstring explaining why this bug happens" as
an illustration — is classified `debug`, because that is exactly what the text
says. The draft still happens on rung 0 either way, but `verify_rung` defaults
to the dearest rung the tasks imply, so a mis-inferred batch gets verified at
rung 3 instead of rung 1. Pass `verify_rung` explicitly when the prompts
discuss other kinds of work.

Warm the model first for anything sizeable — a cold 18 GB model costs ~30 s to
page in against ~0.3 s warm:

```
ladder_models(action="warm")
```

Key options:

- `verify_rung` — which tier judges. Defaults to the dearest rung the tasks
  themselves imply, because verifying below that approves answers the real
  target might have rejected.
- `chunk` — drafts per verification call (default 12). Larger is cheaper.
- `pipeline` — draft the next chunk while the current one is verified
  (default on). The local box is otherwise idle for the whole round trip.
