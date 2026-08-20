# Eval Methodology

## Overview

The eval harness is the single loudest signal in the whole submission. Almost nobody builds one.

We simulate ~5,000 payment failures through four policies — no retry, fixed 3-retry, XGBoost/rules, and LLM agent — across multiple random seeds, and report recovery rate, retry cost, false-retry rate, and time-to-recovery with variance shown.

## Bank Response Simulator

**What it models:** Per-bank × per-rail × per-hour success probabilities for major Indian banks.

**Key assumptions (all synthetic):**
- Base success rates: UPI 85-92%, credit card 88-95%, debit card 82-90%, netbanking 78-88%
- Time-of-day modifiers: 0.85x at night (0-6 AM), 1.0x during peaks (10 AM-2 PM, 5-9 PM)
- Failure-class retry modifiers: network_error has 0.85 same-rail success, insufficient_funds has 0.30
- Retry decay: 0.9x per subsequent attempt
- Delay bonus: bank_downtime improves with longer delays (up to +30%)

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
| ₹ per ₹1Cr Failed | Revenue recovered per crore of failed volume | The business metric |

## Statistical Methodology

- **Multiple seeds:** Default 5 seeds (42-46), configurable
- **Variance:** Mean ± standard deviation reported for all metrics
- **Reproducibility:** All randomness goes through seeded numpy RandomState
- **Independence:** Each seed generates fresh scenarios AND fresh simulator state

## How to Extend with Real Data

1. Replace `eval/bank_profiles.py` with real bank success rates from Razorpay analytics
2. Calibrate the scenario generator with real failure distributions
3. Replace customer nudge response rates with A/B test data
4. Add a real-data eval mode that replays actual webhook events through the pipeline
