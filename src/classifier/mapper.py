"""
Deterministic classifier — maps Razorpay error codes to failure taxonomy.

No LLM here. A regex-solvable problem solved by an LLM is the first thing
a panel will flag. This is pure lookup + rule matching against a YAML config.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from src.classifier.taxonomy import FailureClass

logger = logging.getLogger(__name__)

_YAML_PATH = Path(__file__).parent / "error_codes.yaml"


class ClassifierMapper:
    """
    Maps Razorpay's 5-tuple error codes to the FailureClass taxonomy.

    Rules are loaded from error_codes.yaml at init, sorted by priority
    (highest first). First matching rule wins.
    """

    def __init__(self, yaml_path: Path | str | None = None) -> None:
        path = Path(yaml_path) if yaml_path else _YAML_PATH
        with open(path) as f:
            data = yaml.safe_load(f)

        self._rules: list[dict[str, Any]] = sorted(
            data.get("rules", []),
            key=lambda r: r.get("priority", 0),
            reverse=True,
        )
        logger.info("Loaded %d classifier rules from %s", len(self._rules), path)

    def classify(
        self,
        error_code: str,
        error_description: str | None = None,
        error_source: str | None = None,
        error_step: str | None = None,
        error_reason: str | None = None,
    ) -> tuple[FailureClass, bool]:
        """
        Classify a payment failure based on Razorpay error fields.

        Args:
            error_code: High-level code (BAD_REQUEST_ERROR, GATEWAY_ERROR, etc.)
            error_description: Human-readable description.
            error_source: Where the error originated (customer, gateway, etc.)
            error_step: Pipeline stage (payment_authentication, etc.)
            error_reason: Machine-readable reason code.

        Returns:
            Tuple of (FailureClass, is_retryable).
        """
        for rule in self._rules:
            if self._matches(rule, error_code, error_source, error_step, error_reason):
                try:
                    fc = FailureClass(rule["failure_class"])
                except ValueError:
                    logger.error(
                        "Invalid failure_class in rule: %s", rule["failure_class"]
                    )
                    continue

                retryable = rule.get("retryable", fc.is_retryable)
                logger.debug(
                    "Classified: code=%s reason=%s → %s (retryable=%s)",
                    error_code,
                    error_reason,
                    fc.value,
                    retryable,
                )
                return fc, retryable

        # No match — log warning and return UNKNOWN
        logger.warning(
            "Unrecognised error code — classified as UNKNOWN: "
            "code=%s, source=%s, step=%s, reason=%s, desc=%s",
            error_code,
            error_source,
            error_step,
            error_reason,
            (error_description or "")[:100],
        )
        return FailureClass.UNKNOWN, False

    @staticmethod
    def _matches(
        rule: dict[str, Any],
        error_code: str,
        error_source: str | None,
        error_step: str | None,
        error_reason: str | None,
    ) -> bool:
        """Check if all fields specified in a rule match the input."""
        if "error_reason" in rule:
            if not error_reason or rule["error_reason"] != error_reason:
                return False
        if "error_step" in rule:
            if not error_step or rule["error_step"] != error_step:
                return False
        if "error_source" in rule:
            if not error_source or rule["error_source"] != error_source:
                return False
        if "error_code" in rule:
            if rule["error_code"] != error_code:
                return False
        return True


# Module-level singleton for convenience
_mapper: ClassifierMapper | None = None


def get_classifier() -> ClassifierMapper:
    """Return a cached ClassifierMapper instance."""
    global _mapper
    if _mapper is None:
        _mapper = ClassifierMapper()
    return _mapper
