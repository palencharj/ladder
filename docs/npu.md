# The Intel NPU: measured, and not worth using

Short answer: **the NPU is slower than the CPU for this workload — about 8×
slower on the same model — and Ladder does not use it.** This page records the
measurement so nobody has to repeat it.

Every Intel Core Ultra has an "AI Boost" NPU, and reaching for it is a
reasonable instinct when local inference feels slow. The instinct is wrong here,
for reasons worth understanding.

## What was measured

Machine: Intel Core Ultra 7 265U, no discrete GPU, 64 GB DDR5-5600.
NPU: Intel AI Boost, device `7D1D`, architecture 3720, driver 32.0.100.4778.
OpenVINO 2026.3 / openvino-genai 2026.3.1, which detects the NPU correctly.

Identical prompt throughout — ~374 tokens of triage context, a one-word answer.
That shape was chosen deliberately: it is the "lots of small cheap requests"
case, which is where an NPU should look best. Median of three warm runs.

| Engine | Model | Median wall |
|---|---|---|
| Ollama | `qwen3-coder:30b` (MoE) | **0.45 s** |
| Ollama | `qwen2.5-coder:3b` | 0.56 s |
| OpenVINO CPU | `Qwen2.5-1.5B-int4` | **0.31 s** |
| OpenVINO **NPU** | `Qwen2.5-1.5B-int4` | **2.62 s** |

All four returned the correct answer.

The NPU is **8× slower than the same model on the CPU**, and roughly 6× slower
than the 30B Ladder already runs through Ollama — a model twenty times its size.

## Why the intuition fails

Two arguments were made for the NPU before measuring, and both were wrong.

**"Generation is bandwidth-bound, so a compute accelerator will not help."**
Correct as far as it goes, but incomplete — it predicted *no gain*, not a loss.

**"But small requests are prefill-dominated, and prefill is compute-bound, so
the NPU should shine."** Reasonable, and still wrong. For a ~374-token prompt
the prefill work is real, but it is nowhere near enough to amortise the NPU's
fixed costs: getting the graph and weights across to the accelerator, static
shape handling, and dispatch overhead per inference. NPUs are built for
sustained fixed-shape inference at low power — vision models processing frame
after frame. A one-shot short generation is close to the worst case for them.

The lesson is not that either argument was stupid. It is that both were
predictions, and a twenty-minute experiment settled what neither could.

## Other practical obstacles

Even had it been faster, adopting it would have cost more than it returned:

- **Ollama has no NPU backend at all.** Only CUDA, ROCm, Metal and CPU. Using
  the NPU means a separate runtime, so `ollama pull` and its model management
  stop applying.
- **Models must be converted** to OpenVINO IR with INT4 symmetric weights.
- **Not every converted model compiles.** `qwen2.5-coder-1.5b-instruct-int4-ov`
  failed NPU compilation outright with `StopLocationVerifierPass ... Found 142
  duplicated names`, while `Qwen2.5-1.5B-Instruct-int4-ov` compiled fine. There
  is no way to know which is which without trying.
- **Compilation takes ~35 s** the first time a model is loaded for the NPU.
- **`OPTIMAL_NUMBER_OF_INFER_REQUESTS = 1`** — no concurrency.

## Where the speed actually came from

The thing that made local inference feel fast was not an accelerator. It was
**keeping the model resident**: a cold 18 GB model costs ~33 s to page in
against 0.3 s warm. Ladder sends `keep_alive` (30 minutes by default,
`LADDER_KEEP_ALIVE` to change), and that single change is worth more than
anything the NPU offered.

See [`cost-model.md`](cost-model.md) for the full local performance picture.

## Reproducing this

```bash
pip install openvino-genai huggingface_hub truststore
```

On a corporate network doing TLS inspection, HuggingFace downloads fail with
`CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`.
Python does not trust the corporate root CA that Windows does. Fix it by using
the OS certificate store:

```python
import truststore
truststore.inject_into_ssl()
```

Then download a pre-converted INT4 model and compare devices:

```python
import openvino_genai as ov_genai
pipe = ov_genai.LLMPipeline(model_dir, "NPU")   # or "CPU", "GPU"
print(pipe.generate(prompt, ov_genai.GenerationConfig(max_new_tokens=8)))
```

If you run this on different hardware — particularly a Lunar Lake or Panther
Lake part, whose NPUs are several times larger than the 3720 here — a PR adding
your numbers would be genuinely useful. The conclusion above is specific to
this class of machine, not a claim about NPUs in general.
