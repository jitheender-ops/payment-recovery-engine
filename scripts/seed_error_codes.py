"""
Validate error_codes.yaml — against the FailureClass enum, and against
Razorpay's own published list of decline reasons.

The second check is the one with teeth, and it exists because the first one
could never have caught the bug it was added for. On 2026-09-01 ten of the
eighteen reasons Razorpay documents matched no rule in this file. Every rule
present was perfectly valid, so this script said "✅ All rules reference
valid failure classes" — while an unmatched reason fell through to
FailureClass.UNKNOWN, which is non-retryable, which means the case was
abandoned without a single attempt. The gap was invisible precisely because
it was an ABSENCE of rules, and nothing was checking for absence.

Exits non-zero on failure. It did not before: main() returned None whatever
it found, so run.sh's `&& ok "..."` printed a tick unconditionally and the
gate could not fail.
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.classifier.mapper import ClassifierMapper  # noqa: E402
from src.classifier.taxonomy import FailureClass  # noqa: E402

YAML_PATH = Path(__file__).parent.parent / "src" / "classifier" / "error_codes.yaml"


def main() -> int:
    with open(YAML_PATH) as f:
        data = yaml.safe_load(f)

    rules = data.get("rules", [])
    documented: dict[str, bool] = data.get("razorpay_documented", {})
    acknowledged: dict[str, str] = data.get("razorpay_unmapped_deliberately", {})
    test_cards: dict[str, str] = data.get("razorpay_test_cards", {})
    valid_classes = {fc.value for fc in FailureClass}
    errors: list[str] = []
    class_coverage = set()

    for i, rule in enumerate(rules):
        fc = rule.get("failure_class")
        if fc not in valid_classes:
            errors.append(f"Rule {i}: invalid failure_class {fc!r}")
        else:
            class_coverage.add(fc)

    print(f"Total rules: {len(rules)}")
    print(f"Valid classes covered: {len(class_coverage)}/{len(valid_classes) - 1}")

    uncovered = valid_classes - class_coverage - {"unknown"}
    if uncovered:
        print(f"⚠️  Classes no rule can produce: {sorted(uncovered)}")

    # Every reason Razorpay publishes must be matched BY NAME.
    #
    # The probe deliberately passes an empty error_code and no source/step, so
    # only rules keyed on error_reason can fire. Passing a realistic 5-tuple
    # instead would be useless: the low-priority catch-alls (BAD_REQUEST_ERROR
    # + customer, GATEWAY_ERROR, ...) swallow anything unmatched into
    # issuer_decline or network_error, so nothing would ever look unmapped and
    # this gate would pass while a reason was being silently misclassified.
    # That is precisely how ten of them hid until 2026-09-01.
    #
    # The cost is that a reason legitimately handled by a companion field —
    # `authentication_failed`, which needs error_step payment_authentication —
    # must ALSO carry a bare error_reason rule to pass. That is the right
    # trade: an explicit rule per documented reason is what makes this
    # checkable at all.
    mapper = ClassifierMapper()
    unmapped: list[str] = []
    known_gaps: list[str] = []
    disagreements: list[str] = []
    for reason, rzp_retryable in sorted(documented.items()):
        fc, ours = mapper.classify("", None, None, None, reason)
        if fc is FailureClass.UNKNOWN:
            (known_gaps if reason in acknowledged else unmapped).append(reason)
        elif ours != rzp_retryable:
            disagreements.append(
                f"{reason}: we say retryable={ours} ({fc.value}), "
                f"Razorpay says {rzp_retryable}"
            )

    # The forcing test cards are a SECOND reference, checked the same way but
    # reported separately — they are strings the gateway is observed to emit,
    # where `documented` is what Razorpay publishes. Three of these reached
    # no rule on 2026-09-01 while the documented list was already complete,
    # so one source does not stand in for the other.
    card_unmapped: list[str] = []
    for reason in sorted(test_cards):
        fc, _ = mapper.classify("", None, None, None, reason)
        if fc is FailureClass.UNKNOWN and reason not in acknowledged:
            card_unmapped.append(reason)
    if card_unmapped:
        errors.append(
            "these forcing-test-card reasons reach no rule, so a real test "
            "payment would be misclassified: " + ", ".join(card_unmapped)
        )

    print(f"Razorpay-documented reasons: {len(documented)}")
    print(f"Forcing test-card reasons:   {len(test_cards)}")
    if unmapped:
        errors.append(
            "these documented reasons classify as UNKNOWN, so cases carrying "
            "them are abandoned without an attempt: " + ", ".join(unmapped)
        )
    # Not an error. A disagreement can be the right call — payment_cancelled
    # is deliberately non-retryable here because the customer said stop — but
    # it must be a decision someone made, so it is printed every run and
    # justified in docs/decline-taxonomy.md.
    # Printed every run so an accepted gap stays visible instead of becoming
    # invisible the moment it is written down.
    if known_gaps:
        print("\nDocumented reasons deliberately unmapped (see the YAML for why):")
        for reason in known_gaps:
            print(f"  · {reason}")

    if disagreements:
        print("\nDeliberate disagreements with Razorpay's retry verdict")
        print("(see docs/decline-taxonomy.md for the reasoning):")
        for d in disagreements:
            print(f"  · {d}")

    if errors:
        print()
        for e in errors:
            print(f"❌ {e}")
        return 1
    print("\n✅ Every rule is valid and every documented reason is classified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
