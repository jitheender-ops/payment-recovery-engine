# Eval Methodology

## Overview

The eval harness is the single loudest signal in the whole submission. Almost nobody builds one.

We simulate ~5,000 payment failures through four policies — no retry, fixed 3-retry, XGBoost/rules, and LLM agent — across multiple random seeds, and report recovery rate, retry cost, false-retry rate, and time-to-recovery with variance shown.

## Bank Response Simulator

**What it models:** Per-bank × per-rail × per-hour success probabilities for major Indian banks.

**The central assumption — a retry is not a fresh payment.**

The payment already failed for a specific reason, so the bank's baseline
approval rate (calibrated on random *new* payments) says very little about
whether a retry works. What matters is whether the cause went away:

```
P(retry succeeds) = P(blocker cleared) x P(payment clears | blocker gone)
```

The second term is the bank/rail/hour baseline. The first is `CONDITION_CLEARS`
in `eval/bank_profiles.py`, and it is what an earlier version of this model got
wrong — it scored retries at near-baseline rates and reported ~73% blended
recovery against a real world nearer 15-30%.

**Key assumptions (all synthetic):**
- Base success rates: UPI 85-92%, credit card 88-95%, debit card 82-90%, netbanking 78-88%
- Time-of-day modifiers: 0.85x at night (0-6 AM), 1.0x during peaks (10 AM-2 PM, 5-9 PM)
- `P(blocker cleared)`, same rail: insufficient_funds 0.05, card_limit_exceeded 0.04,
  issuer_decline 0.07, 3ds_dropoff 0.13, bank_downtime 0.20, network_error 0.60
- Switching rail or nudging the customer is what moves those numbers — e.g.
  issuer_decline 0.07 -> 0.30 on a different rail, insufficient_funds 0.05 -> 0.26
  after a nudge. This is the entire source of the agent's edge over fixed retry.
- Retry decay: 0.6x per subsequent attempt — each failure is evidence the blocker
  is persistent rather than transient
- Bank downtime recovers on an exponential curve with a 45-minute half-life,
  toward the switched-rail ceiling. Applied once; an earlier version applied both
  a delay multiplier and an additive delay bonus, rewarding the same wait twice
- Unmodelled failure classes are treated pessimistically (0.15x), so an unknown
  blocker never inherits the full baseline approval rate

**Calibration knob:** `RETRY_CALIBRATION` (currently 0.62) scales all of the
above. The per-class numbers encode relative ordering — which failures are
recoverable and by which lever — and the scalar anchors the blend to the
published band. Tune the scalar, not the individual classes, when re-anchoring.
`tests/test_calibration.py` fails if blended baseline recovery leaves 15-30%.

**What it does NOT model:**
- Correlated bank failures across rails
- Seasonal patterns (festival season, salary dates)
- Customer-specific behavior patterns
- Gateway-level routing decisions

## Scenario Generation

Distributions modelled on Indian payment patterns:
- **Amount:** Log-normal, median ~₹500, range ₹10-₹1L
- **Method:** UPI 55%, card 35%, netbanking 8%, wallet 2%
- **Bank:** Weighted by market share (SBI 25%, HDFC 18%, ICICI 15%, etc.)
- **Failure class:** insufficient_funds 20%, 3ds_dropoff 15%, bank_downtime 12%, etc.
- **Time of day:** Mixture of peaks at 10 AM, 1 PM, 8 PM
- **Customers:** ~2,000 unique (some have multiple failures)

## Metrics

| Metric | Definition | Why It Matters |
|--------|-----------|----------------|
| Recovery Rate | % of failed payments eventually recovered | The headline number |
| Retry Cost | Avg retry attempts per scenario | Lower is better — fewer API calls, less customer fatigue |
| False-Retry Rate | % of retries on non-retryable failures | Measures intelligence — dumb policies retry everything |
| Time-to-Recovery | Median minutes from failure to recovery | Speed matters for conversion |
| ₹ per ₹1Cr Failed | Revenue recovered per crore of failed volume | Gross business metric |
| **Net ₹ per ₹1Cr** | Revenue recovered minus `attempts x cost_per_retry` | **The headline.** Gross alone makes brute force optimal by construction |
| Break-even /retry | Retry cost at which a policy ties the baseline | Removes the cost assumption from the conclusion entirely |

### On pricing a retry

The default is ₹2.00/attempt (`--retry-cost-inr`), covering gateway and ops cost
only. It deliberately excludes the two costs that dominate in practice and that
we cannot source defensibly: penalties merchants incur for a poor decline ratio,
and customer goodwill lost to repeated failed-payment messages. Both push the
true figure higher, so treat ₹2.00 as a floor.

Rather than defend that number, the harness reports the break-even cost at which
two policies tie. A policy that recovers more *while* attempting fewer dominates
at every possible cost including zero, and the assumption never has to be argued.

## Statistical Methodology

- **Multiple seeds:** Default 5 seeds (42-46), configurable
- **Variance:** Mean ± standard deviation reported for all metrics
- **Reproducibility:** All randomness goes through seeded numpy RandomState
- **Common random numbers (paired comparison):** each scenario's RNG stream is
  derived from `(seed, scenario_id)`, so scenario 417 draws the identical
  sequence no matter which policy is deciding. Two policies taking the same
  action get the same outcome, and the only variance left between them is caused
  by their decisions.

  This matters more than it sounds. Comparing two independent means cannot
  resolve a sub-1pp difference when seed-to-seed σ is itself ~0.5pp. Pairing lets
  us difference outcomes one-to-one across 25,000 paired scenarios and put a 95%
  CI on the *difference*. A result is only called real when that interval
  excludes zero — reported per metric in `eval/results/eval_results.md`.

  Without pairing, each policy ran on a different slice of one shared RNG stream,
  and the between-policy difference was confounded with raw sampling noise.

## How to Extend with Real Data

1. Replace `eval/bank_profiles.py` with real bank success rates from Razorpay analytics
2. Calibrate the scenario generator with real failure distributions
3. Replace customer nudge response rates with A/B test data
4. Add a real-data eval mode that replays actual webhook events through the pipeline
