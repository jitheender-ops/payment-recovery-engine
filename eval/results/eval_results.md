# Eval Harness Results

Scenarios: 5000 | Seeds: 5

| Policy        |   Recovery Rate (%) |   Retry Cost (avg) |   False-Retry (%) |   Time-to-Recovery (min) | ₹ per ₹1Cr Failed   |
|---------------|---------------------|--------------------|-------------------|--------------------------|---------------------|
| No Retry      |                 0   |               0    |                 0 |                      inf | ₹0                  |
| Fixed 3-Retry |                73.5 |               1.88 |                 8 |                       15 | ₹7,236,492          |
| XGBoost/Rules |                72.8 |               1.61 |                 0 |                        0 | ₹7,256,487          |

*Variance (±σ) shown in JSON results.*

## Paired comparison vs. Fixed 3-Retry

Common random numbers: each scenario draws the identical random sequence
under every policy, so outcomes are differenced one-to-one. A difference is
only called real when the 95% CI excludes zero.

| Policy | Metric | Δ vs baseline | 95% CI | n | Real? |
|---|---|---|---|---|---|
| No Retry | recovery_rate_pp | -73.452 | [-73.999, -72.905] | 25,000 | yes |
| No Retry | retry_cost | -1.878 | [-1.889, -1.867] | 25,000 | yes |
| XGBoost/Rules | recovery_rate_pp | -0.668 | [-1.347, +0.011] | 25,000 | no — inside noise |
| XGBoost/Rules | retry_cost | -0.262 | [-0.273, -0.251] | 25,000 | yes |
