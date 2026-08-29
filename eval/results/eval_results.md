# Eval Harness Results

Scenarios: 5000 | Seeds: 5

| Policy        |   Recovery Rate (%) |   Retry Cost (avg) |   False-Retry (%) | Time-to-Recovery (min)   | ₹ per ₹1Cr Failed   | Net ₹ per ₹1Cr   |
|---------------|---------------------|--------------------|-------------------|--------------------------|---------------------|------------------|
| No Retry      |                 0   |               0    |                 0 | —                        | ₹0                  | ₹0               |
| Fixed 3-Retry |                19.2 |               2.73 |                 8 | 15                       | ₹1,849,489          | ₹1,795,744       |
| XGBoost       |                30.2 |               2.32 |                 0 | 0                        | ₹3,011,938          | ₹2,966,249       |
| LLM Agent     |                26.6 |               2.38 |                 0 | 0                        | ₹2,644,430          | ₹2,597,603       |

*Variance (±σ) shown in JSON results.*

## Paired comparison vs. Fixed 3-Retry

Common random numbers: each scenario draws the identical random sequence
under every policy, so outcomes are differenced one-to-one. A difference is
only called real when the 95% CI excludes zero.

| Policy | Metric | Δ vs baseline | 95% CI | n | Real? |
|---|---|---|---|---|---|
| No Retry | recovery_rate_pp | -19.212 | [-19.700, -18.724] | 25,000 | yes |
| No Retry | retry_cost | -2.729 | [-2.737, -2.721] | 25,000 | yes |
| XGBoost | recovery_rate_pp | +11.032 | [+10.543, +11.521] | 25,000 | yes |
| XGBoost | retry_cost | -0.409 | [-0.421, -0.397] | 25,000 | yes |
| LLM Agent | recovery_rate_pp | +7.340 | [+6.897, +7.783] | 25,000 | yes |
| LLM Agent | retry_cost | -0.351 | [-0.363, -0.340] | 25,000 | yes |

## Retry economics vs. Fixed 3-Retry

Net figures charge ₹2.00 per retry attempt. The break-even
column removes that assumption: it is the cost at which the two policies tie,
so the conclusion holds without agreeing on what a retry actually costs.

| Policy | Δ revenue /Cr | Δ attempts /Cr | Break-even /retry | Verdict |
|---|---|---|---|---|
| XGBoost | ₹+1,162,449 | -4,029 | — | dominates at any retry cost (incl. ₹0) |
| LLM Agent | ₹+794,941 | -3,459 | — | dominates at any retry cost (incl. ₹0) |
