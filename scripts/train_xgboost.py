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

Every run also writes <output>.card.json — the model card. The joblib is a
1MB binary with no provenance, so "is this model stale? trained on what?" had
no answer once it left the machine that trained it. The card answers it:
what trained it (taxonomy, action space, feature width — the things a model
is silently FROZEN at, see xgboost_baseline's width refusal), when, how many
samples, from which seed, at what retry cost, and the SHA-256 to pin in
XGBOOST_MODEL_SHA256. Compared against the live taxonomy at load time, a
stale model names itself before it can mislabel a decision.
"""
import argparse
import sys
from pathlib import Path
from typing import Any

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

    counts: dict[str, int] = {
        ACTION_LABELS[i]: int((y == i).sum()) for i in range(len(ACTION_LABELS))
    }
    print(f"Label distribution (expected-value argmax): {counts}")

    # XGBoost requires every label in [0, num_class) to be present; retry_now
    # wins the argmax only in a narrow corner of the scenario space (~0.06%),
    # so tiny runs can legitimately produce a class of zero. Fail with the
    # fix stated rather than xgboost's "Invalid classes inferred" riddle.
    missing = [ACTION_LABELS[i] for i in range(len(ACTION_LABELS)) if counts[ACTION_LABELS[i]] == 0]
    if missing:
        raise SystemExit(
            f"No training examples for: {', '.join(missing)}. "
            f"Raise --n-samples (5000 keeps every class populated; "
            f"retry_now is the scarce one)."
        )

    # Held out, and stratified so a rare class does not vanish from the test
    # split entirely and report an undefined score.
    #
    # The check that matters happens AFTER the split: predict() maps its output
    # index through ACTION_LABELS, so a model fitted on anything less than the
    # full label space would mislabel every prediction by one position —
    # silently. A run whose training half lost a class must stop here, loudly.
    min_class = min(int((y == i).sum()) for i in np.unique(y))
    stratify = y if min_class >= 2 else None
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=args.test_size, random_state=args.seed, stratify=stratify
    )
    lost = [ACTION_LABELS[i] for i in range(len(ACTION_LABELS)) if i not in np.unique(y_train)]
    if lost:
        raise SystemExit(
            f"Training split is missing classes: {', '.join(lost)}. "
            f"A model fitted on a partial label space would mislabel every "
            f"prediction through ACTION_LABELS. Raise --n-samples "
            f"(5000 keeps every class populated; retry_now is the scarce one)."
        )
    print(f"Training on {len(X_train)}, holding out {len(X_test)}...")

    from src.agent.xgboost_baseline import XGBoostBaseline
    # "" forces the rule path so training never loads a previous model over
    # itself — an empty string is falsy, unlike None which means "use settings".
    model = XGBoostBaseline(model_path="").train(X_train, y_train, args.output)

    y_pred = model.predict(X_test)
    labels_present = sorted(set(y_test) | set(y_pred))
    print("\nHeld-out performance:\n")
    report = classification_report(
        y_test, y_pred,
        labels=labels_present,
        target_names=[ACTION_LABELS[i] for i in labels_present],
        zero_division=0,
        output_dict=True,
    )
    print(classification_report(
        y_test, y_pred,
        labels=labels_present,
        target_names=[ACTION_LABELS[i] for i in labels_present],
        zero_division=0,
    ))
    print(f"Model saved to {args.output}")

    write_model_card(args, X.shape[1], counts, report)


def write_model_card(
    args: argparse.Namespace,
    feature_width: int,
    label_counts: dict[str, int],
    heldout_report: dict[str, Any],
) -> None:
    """
    The provenance record next to the joblib, as JSON.

    Everything here is either a fact the training run already had or a hash of
    code the model is frozen against. The card is written AFTER the joblib and
    records the joblib's own SHA-256, so the pair is verifiable: pin the digest
    in XGBOOST_MODEL_SHA256 and the loader refuses any file whose bytes were
    swapped without a matching card.
    """
    import hashlib
    import json

    from src.agent.xgboost_baseline import FAILURE_CLASSES, METHODS

    model_path = Path(args.output)
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()

    card = {
        "model": model_path.name,
        "sha256": digest,
        "trained_at": datetime.now(UTC).isoformat(),
        "label_source": "expected-value argmax over the bank simulator "
                        "(eval/bank_profiles.py), not the rule heuristic",
        "samples": args.n_samples,
        "train_samples": int(args.n_samples * (1 - args.test_size)),
        "test_size": args.test_size,
        "seed": args.seed,
        "retry_cost_inr": args.retry_cost_inr,
        "feature_width": feature_width,
        "failure_classes": list(FAILURE_CLASSES),
        "methods": list(METHODS),
        "action_labels": list(ACTION_LABELS),
        "label_counts": label_counts,
        "heldout": {
            name: {m: round(float(v), 4) for m, v in stats.items()}
            for name, stats in heldout_report.items()
            if isinstance(stats, dict)
        },
        "pin_hint": f"XGBOOST_MODEL_SHA256={digest}",
    }
    card_path = model_path.with_suffix(".card.json")
    card_path.write_text(json.dumps(card, indent=2) + "\n")
    print(f"Model card written to {card_path}")
    print(f"Pin this in .env: XGBOOST_MODEL_SHA256={digest}")


if __name__ == "__main__":
    main()
