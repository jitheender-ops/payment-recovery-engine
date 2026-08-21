"""
Schema validation for policy agent output.

Validates that agent output conforms to the RetryAction schema with
additional semantic checks (e.g., rail required for switch_rail).
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Any

from pydantic import ValidationError

from src.agent.actions import RetryAction

logger = logging.getLogger(__name__)


def validate_action_schema(
    action_dict: dict[str, Any],
) -> tuple[bool, RetryAction | None, str | None]:
    """
    Validate an action dictionary against the RetryAction schema.

    Returns:
        (is_valid, parsed_action_or_None, error_message_or_None)
    """
    try:
        action = RetryAction(**action_dict)
    except ValidationError as e:
        return False, None, f"Schema validation failed: {e}"
    except Exception as e:
        return False, None, f"Unexpected validation error: {e}"

    # Semantic checks
    if action.action == "switch_rail" and action.rail is None:
        return False, None, "switch_rail action requires a target rail"

    if action.action == "retry_at" and action.retry_at is None:
        return False, None, "retry_at action requires a retry_at timestamp"

    if action.action == "retry_at" and action.retry_at is not None:
        from datetime import datetime
        now = datetime.now(UTC)
        if action.retry_at < now:
            return False, None, f"retry_at timestamp is in the past: {action.retry_at}"

    return True, action, None
