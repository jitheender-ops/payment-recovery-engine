# Post-Vulcan eval failure-mix reweighting + per-class metrics

**Status: IMPLEMENTED 2026-09-03.** All checks green (ruff, mypy --strict,
833 tests). No production code touched — `eval/` and docs only.

## Context

Razorpay launched **Vulcan** (Aug 18, 2026): a transformer-based payments
foundation model trained on ~3 trillion data points across 4 billion
payments, embedded in their authorization-time stack (routing, risk
scoring, success prediction). Public claims: 8–10% success-rate
improvement, 5X disputed-transaction identification, 8X international
card-fraud detection.

Vulcan sits **before** the transaction; our engine sits **after it fails**.
That complementarity has a consequence for the eval harness: the harness
draws its scenarios from a fixed `FAILURE_WEIGHTS` distribution
(`eval/scenario_generator.py:16`) that models the *pre-Vulcan* failure
population. If Vulcan compresses self-clearing transient failures, the
conditional distribution of "failures that actually reach a recovery
engine" shifts toward the harder classes — and every number the harness
produces quietly stops describing the world the engine will live in.

---

## What used to be the case

One mix, one dict, blended metrics:

1. **`FAILURE_WEIGHTS` was the only population.** Thirteen classes,
   weighted by assumed real-world frequency: 0.42 of mass on the four
   same-rail transients (`bank_downtime` 0.12, `network_error` 0.12,
   `upi_collect_timeout` 0.10, `payment_timeout` 0.08) that a
   retry-anything policy recovers by accident.

2. **`risk_check_failed` was never emitted.** It is in the taxonomy
   (`src/classifier/taxonomy.py` — the switch-only class, whose
   `retry_now`/`retry_at` the guardrail rejects in
   `check_switch_only_class`) but not in `FAILURE_WEIGHTS`, so no eval
   scenario ever exercised the switch-only path. The one failure class
   where a policy's *discipline* (not retrying the same rail) is the
   entire question had zero representation in the harness.

3. **Results were blended-only.** `eval/runner.py` reported a single
   recovery rate per policy. A mix change moves blended numbers for
   composition reasons alone (Simpson's paradox): a harder mix drags
   every policy down, and the headline table cannot distinguish
   "policy got worse" from "population got harder". Nor could you see
   *where* a policy's edge lived — e.g. that XGBoost's advantage is
   concentrated in judgment-call classes while Fixed 3-Retry is already
   right on `network_error`.

4. **No provenance on saved results.** `eval_results.json` did not
   record which population produced it, so a future "vulcan" run would be
   indistinguishable from a "legacy" run once both sat on disk.

## What caused the (conceptual) error

Not a bug — an unstated assumption aging badly. The mix encoded
"failures as India produces them"; Vulcan changes "failures as they
arrive at a recovery engine sitting behind a Vulcan-equipped gateway."
The specific hypothesised shifts:

- Transients that clear themselves (network blips, timeouts, downtime)
  are exactly what an authorization-time optimizer suppresses first.
- What survives skews to blockers only a *recovery engine* can move:
  insufficient funds (nudge/top-up), issuer declines and 3DS dropoff
  (rail switch), card limits (alternate instrument).
- Vulcan's 8X fraud detection raises the share of `fraud_block` reaching
  us as terminal, non-retryable failures.
- `risk_check_failed` becomes reachable (a risk screen stopping the
  instrument while the customer is still good for a different one).

Nobody has measured the post-Vulcan conditional distribution — these
weights are a hypothesis, marked as one in the code
(`ponytail: guessed mix, replace with observed payment_failures
distribution once deployed`).

## What changed

### `eval/scenario_generator.py`

- `MIXES: dict[str, dict[str, float]]` — named population profiles:

  - `"legacy"` — the original `FAILURE_WEIGHTS`, byte-for-byte. Every
    pre-existing result stays reproducible; the default.
  - `"vulcan"` — the hypothesised post-Vulcan conditional mix:
    transients 0.42 → 0.21 combined, `insufficient_funds` 0.20 → 0.26
    (modal failure), rail-switch cases roughly doubled,
    `risk_check_failed` added at 0.01 so the switch-only path is
    finally exercised.

- `ScenarioGenerator.__init__` takes `mix: str = "legacy"`, validates
  the name, and copies (not references) the chosen weights into
  `self.failure_weights` — an instance cannot mutate the legacy profile.
- `FAILURE_WEIGHTS` itself is untouched; legacy importers unaffected.

### `eval/runner.py`

- `--mix {legacy,vulcan}` CLI flag (argparse `choices`-validated), an
  `EvalRunner(mix=...)` constructor arg, and the mix name stamped into
  three places: the console banner, `eval_results.json` (`"mix"` key),
  and the markdown header — every result file now self-describes its
  population.
- `run_policy` results now carry `failure_class` per scenario row.
- `_per_class_breakdown()` — seed-aggregated recovery% and avg attempts
  per (policy × failure_class), exposed as:
  - `per_failure_class` in `eval_results.json`,
  - a "Recovery by failure class" table in the markdown + console
    (`print_per_class()`).

  This is the Simpson's-paradox guard: when the mix changes and blended
  numbers move, this table is how you tell composition shift from
  policy regression — and where an LLM agent's edge should concentrate
  (judgment-call classes) if it is real.

### Deliberately NOT changed

- `CONDITION_CLEARS` and `RETRY_CALIBRATION` (`eval/bank_profiles.py`) —
  they encode physics (whether a blocker clears, and by which lever);
  the mix encodes population. Vulcan changes the population, not
  whether insufficient funds clear on a nudge.
- The legacy default. `--mix` is opt-in; every existing trend line
  against old results stays valid without flag archaeology.
- No production code: `src/` untouched, including the switch-only
  guardrail itself.

## How to use it

```bash
# Reproduce any historical result (default mix unchanged):
.venv/bin/python -m eval.runner --skip-llm

# The hypothesised post-Vulcan population:
.venv/bin/python -m eval.runner --skip-llm --mix vulcan
```

Read the "Recovery by failure class" table first, blended numbers
second. The paired comparison and break-even analysis need no change —
all policies run the identical scenarios under a mix, so A/B
conclusions survive any population shift automatically.

## Verification

- `ruff check src eval scripts tests` — clean
- `mypy --strict eval/scenario_generator.py eval/runner.py` — clean
- `pytest -q` — 833 passed
- Smoke runs: `--mix vulcan` (400×2 scenarios) emits `risk_check_failed`
  (n=9) and the per-class table renders; `--mix legacy` output is
  byte-compatible with the old distribution (no `risk_check_failed`,
  `mix: legacy` stamped in JSON).

## Still open

- Replace the guessed vulcan weights with the observed distribution:
  `payment_failures` is already indexed on `failure_class`
  (`ix_payment_failures_class`), so one `GROUP BY failure_class`
  query against production data replaces the hypothesis with fact.
- The per-class table is per-policy; a per-class *paired* delta (vs
  Fixed 3-Retry, same common-random-numbers trick) would tighten it
  further if the aggregated table ever proves ambiguous.
