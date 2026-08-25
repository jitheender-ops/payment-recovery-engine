# Agent instructions — Payment Failure Recovery Engine

A knowledge graph of this codebase lives in `graphify-out/` (gitignored,
regenerable). Use it before grepping.

## Codebase questions

Run graphify first; fall back to file search only when the answer is missing:

```bash
graphify query "how does revenue attribution work?"
graphify explain "PaymentRecoveryOrchestrator"   # one node + its neighbours
graphify path "GuardrailGate" "RetryExecutor"    # how two parts connect
graphify affected "RecoveryCase"                 # what breaks if X changes
graphify god-nodes                               # architectural hubs
```

## After code changes

Keep the graph current so the next session's answers are true:

```bash
graphify update .
```

No LLM key needed. If a refactor deleted code and the rebuild comes back
smaller than the existing graph, pass `--force`.

## Money-path rules (unchanged)

- Never commit secrets; `.env*` is gitignored on purpose.
- The write-ahead ordering in `src/orchestrator.py::_execute_and_record` is a
  correctness property, not a style choice — do not reorder it.
- Guardrail checks all rules and reports all violations; no short-circuiting.
- Verify with `.venv/bin/ruff check src eval scripts tests`,
  `.venv/bin/mypy --strict src scripts eval`, `.venv/bin/python -m pytest -q`.
