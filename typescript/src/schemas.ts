import type { RetryAction } from "./actions.js";

export interface SchemaValidation {
  valid: boolean;
  error: string | null;
}

export function validateActionSchema(
  action: RetryAction,
  now: Date = new Date(),
): SchemaValidation {
  if (action.action === "switch_rail" && (action.rail === undefined || action.rail === null)) {
    return { valid: false, error: "switch_rail action requires a target rail" };
  }

  if (action.action === "retry_at" && (action.retryAt === undefined || action.retryAt === null)) {
    return { valid: false, error: "retry_at action requires a retry_at timestamp" };
  }

  if (action.action === "retry_at" && action.retryAt != null) {
    if (action.retryAt.getTime() < now.getTime()) {
      return {
        valid: false,
        error: `retry_at timestamp is in the past: ${action.retryAt.toISOString()}`,
      };
    }
  }

  return { valid: true, error: null };
}
