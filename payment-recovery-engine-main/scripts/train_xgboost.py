"""Train the XGBoost baseline against simulated OUTCOMES, not against the rules.

Usage: python scripts/train_xgboost.py --n-samples 10000

What changed and why it mattered: this script used to label each scenario with
whatever `XGBoostPolicy.decide()` — a deterministic rule function of the same
features — would have chosen, then score the model on its own training set. A
gradient-boosted tree memorises a deterministic function of its inputs perfectly,
so it reported 1.00 precision and recall on all five classes and the "trained
model" was the rule heuristic with extra steps. The old docstring already said
"find the best action by simulating all options"; the code never did it.

Labels now come from the bank simulator's own probabilities: for each scenario,
compute the expected net recovery of every action in the action space and take
the argmax. That is a signal the rules do not already contain, so the model can
disagree with them — and a held-out split makes the score mean something.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import UTC, datetime

import numpy as np
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split

from eval.bank_profiles import get_bank_profile
from eval.scenario_generator import ScenarioGenerator
from src.agent.actions import FailureContext
from src.agent.xgboost_baseline import ACTION_LABELS, extract_features
from src.config import get_settings
from src.executor.rail_selector import select_alternative_rail

# Hours to wait when the action is retry_at. Long enough for the common
# transient blockers (a topped-up balance, a bank's batch window) to clear.
RETRY_AT_HOURS = 4
# P(customer acts on a nudge at all). Matches the eval simulator's default.
NUDGE_RESPONSE_RATE = 0.60


def expected_values(row: object, retry_cost_paise: int) -> list[float]:
    """
    Expected net recovery in paise for each action in ACTION_LABELS.

    Probabilities, not sampled draws. Sampling would inject RNG noise into the
    labels themselves — the model would be fitting the simulator's coin flips
    rather than its structure, and two runs would disagree about the right
    answer for identical inputs.
    """
    amount = int(row["amount"])              # type: ignore[index]
    bank = str(row["bank"])                  # type: ignore[index]
    rail = str(row["method"])                # type: ignore[index]
    hour = int(row["hour_of_day"])           # type: ignore[index]
    failure_class = str(row["failure_class"])  # type: ignore[index]
    profile = get_bank_profile(bank)

    def p(**kw: object) -> float:
        return profile.get_success_probability(
            rail=kw.get("rail", rail),           # type: ignore[arg-type]
            hour=kw.get("hour", hour),           # type: ignore[arg-type]
            failure_class=failure_class,
            is_retry=True,
            switched_rail=bool(kw.get("switched_rail", False)),
            delay_minutes=int(kw.get("delay_minutes", 0)),  # type: ignore[call-overload]
            after_nudge=bool(kw.get("after_nudge", False)),
        )

    # A hard decline is not a probability question. Retrying a stolen card does
    # not get less wrong with a better expected value, so it is excluded from
    # the arithmetic entirely rather than left to lose on points.
    if not bool(row["is_retryable"]):  # type: ignore[index]
        return [-1.0, -1.0, -1.0, -1.0, 0.0]

    alt = select_alternative_rail(rail, failure_class)
    return [
        p() * amount - retry_cost_paise,                                    # retry_now
        p(hour=(hour + RETRY_AT_HOURS) % 24, delay_minutes=RETRY_AT_HOURS * 60)
        * amount - retry_cost_paise,                                        # retry_at
        (p(rail=alt, switched_rail=True) * amount - retry_cost_paise)
        if alt else -1.0,                                                   # switch_rail
        NUDGE_RESPONSE_RATE * p(after_nudge=True) * amount - retry_cost_paise,
        0.0,                                                                # abandon
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-samples", type=int, default=10000)
    parser.add_argument(
        "--output", type=str, default=get_settings().xgboost_model_path
    )
    parser.add_argument(
        "--retry-cost-inr", type=float, default=2.0,
        help="Cost per attempt. Same default as eval/runner.py.",
    )
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    retry_cost_paise = int(args.retry_cost_inr * 100)

    print(f"Generating {args.n_samples} scenarios...")
    scenarios = ScenarioGenerator(seed=args.seed).generate(args.n_samples)

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
        X_list.append(extract_features(ctx))
        y_list.append(int(np.argmax(expected_values(row, retry_cost_paise))))

    X = np.array(X_list)
    y = np.array(y_list)

    counts = {ACTION_LABELS[i]: int((y == i).sum()) for i in range(len(ACTION_LABELS))}
    print(f"Label distribution (expected-value argmax): {counts}")

    # Held out, and stratified so a rare class does not vanish from the test
    # split entirely and report an undefined score.
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=args.test_size, random_state=args.seed, stratify=y
    )
    print(f"Training on {len(X_train)}, holding out {len(X_test)}...")

    from src.agent.xgboost_baseline import XGBoostBaseline
    # "" forces the rule path so training never loads a previous model over
    # itself — an empty string is falsy, unlike None which means "use settings".
    model = XGBoostBaseline(model_path="").train(X_train, y_train, args.output)

    y_pred = model.predict(X_test)
    labels_present = sorted(set(y_test) | set(y_pred))
    print("\nHeld-out performance:\n")
    print(classification_report(
        y_test, y_pred,
        labels=labels_present,
        target_names=[ACTION_LABELS[i] for i in labels_present],
        zero_division=0,
    ))
    print(f"Model saved to {args.output}")


if __name__ == "__main__":
    main()
