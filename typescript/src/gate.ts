import type { FailureContext, RetryAction } from "./actions.js";
import { validateActionSchema } from "./schemas.js";
import { GuardrailRules, type GuardrailSettings } from "./rules.js";

export interface GuardrailResult {
  passed: boolean;
  action: RetryAction | null;
  rejectionReasons: string[];
  rulesChecked: number;
  rulesFailed: number;
}

export class GuardrailGate {
  private readonly rules: GuardrailRules;

  constructor(settings?: GuardrailSettings) {
    this.rules = new GuardrailRules(settings);
  }

  validate(
    action: RetryAction,
    context: FailureContext,
    idempotencyKey: string,
    currentAttempts: number,
    now: Date = new Date(),
  ): GuardrailResult {
    if (action.action === "abandon") {
      return { passed: true, action, rejectionReasons: [], rulesChecked: 0, rulesFailed: 0 };
    }

    const violations: string[] = [];
    let rulesChecked = 0;

    const add = (check: [boolean, string | null]) => {
      if (!check[0]) violations.push(check[1] ?? "unspecified guardrail violation");
    };

    rulesChecked++;
    const schema = validateActionSchema(action, now);
    if (!schema.valid) violations.push(`Schema: ${schema.error}`);

    rulesChecked++;
    add(this.rules.checkHardDeclineBlocklist(context.failureClass));

    rulesChecked++;
    add(this.rules.checkMaxRetriesPerPayment(context.paymentId, currentAttempts));

    rulesChecked++;
    add(this.rules.checkMaxRetriesPerCustomer(context.retryCount24h));

    rulesChecked++;
    add(this.rules.checkAmountCeiling(context.amount));

    rulesChecked++;
    add(this.rules.checkConsentWindow(context.failedAt, context.currentTime));

    if (action.action === "nudge_customer") {
      rulesChecked++;
      add(this.rules.checkCustomerNudgeRateLimit(context.nudgeCount24h));
    }

    rulesChecked++;
    add(this.rules.checkTimeOfDayBlackout(context.hourOfDay));

    rulesChecked++;
    add(this.rules.checkIdempotencyKey(idempotencyKey));

    const passed = violations.length === 0;
    return {
      passed,
      action: passed ? action : null,
      rejectionReasons: violations,
      rulesChecked,
      rulesFailed: violations.length,
    };
  }
}
