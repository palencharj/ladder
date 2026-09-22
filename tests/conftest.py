import os

# The existing suites exercise rung-0 mechanics (drafting, deflection, batching
# rules) with fake engines -- none of them talks to a real Ollama. Rung 0 is off
# by default now, so they run with it switched back on. The shipped default is
# covered by test_no_local.py, which runs in clean subprocesses without this.
os.environ["LADDER_ENABLE_LOCAL"] = "1"
