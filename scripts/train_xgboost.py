"""Train XGBoost baseline on simulated data.
Usage: python scripts/train_xgboost.py --n-samples 10000 --output models/xgboost_baseline.joblib
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import UTC, datetime

import numpy as np
from sklearn.metrics import classification_report

from eval.policies.xgboost_policy import XGBoostPolicy
from eval.scenario_generator import ScenarioGenerator
from src.agent.actions import FailureContext
from src.agent.xgboost_baseline import ACTION_LABELS, extract_features


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-samples", type=int, default=10000)
    parser.add_argument("--output", type=str, default="models/xgboost_baseline.joblib")
    args = parser.parse_args()

    print(f"Generating {args.n_samples} scenarios...")
    gen = ScenarioGenerator(seed=42)
    scenarios = gen.generate(args.n_samples)
    policy = XGBoostPolicy()

    # For each scenario, find the best action by simulating all options
    X_list, y_list = [], []
    now = datetime.now(UTC)

    for _, row in scenarios.iterrows():
        ctx = FailureContext(
            payment_id=row["payment_id"], failure_class=row["failure_class"],
            error_code="SIM", amount=int(row["amount"]), method=row["method"],
            bank=row["bank"], customer_id=row["customer_id"],
            retry_count_24h=0, nudge_count_24h=0, previous_retry_outcomes=[],
            failed_at=now, current_time=now,
            hour_of_day=int(row["hour_of_day"]), day_of_week=int(row["day_of_week"]),
            is_retryable=row["is_retryable"],
        )
        features = extract_features(ctx)
        decision = policy.decide(row, 0)
        action_idx = (
            ACTION_LABELS.index(decision["action"])
            if decision["action"] in ACTION_LABELS
            else 4
        )
        X_list.append(features)
        y_list.append(action_idx)

    X = np.array(X_list)
    y = np.array(y_list)

    print(f"Training XGBoost on {len(X)} samples...")
    from src.agent.xgboost_baseline import XGBoostBaseline
    baseline = XGBoostBaseline()
    model = baseline.train(X, y, args.output)

    # Evaluate
    y_pred = model.predict(X)
    print("\n" + classification_report(y, y_pred, target_names=ACTION_LABELS))
    print(f"✅ Model saved to {args.output}")

if __name__ == "__main__":
    main()
