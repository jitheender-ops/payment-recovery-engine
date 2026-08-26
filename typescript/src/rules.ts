import { FailureClass, isHardDecline, toFailureClass } from "./taxonomy.js";
import { isInBlackout, type BlackoutSettings } from "./time.js";

export interface GuardrailSettings extends BlackoutSettings {
  maxRetriesPerPayment: number;
  maxRetriesPerCustomer24h: number;
  amountCeilingPaise: number;
  consentWindowHours: number;
  maxNudgesPerCustomer24h: number;
}

export const DEFAULT_GUARDRAIL_SETTINGS: GuardrailSettings = {
  maxRetriesPerPayment: 3,
  maxRetriesPerCustomer24h: 5,
  amountCeilingPaise: 5_000_000,
  consentWindowHours: 72,
  maxNudgesPerCustomer24h: 2,
  retryBlackoutStartHour: 23,
  retryBlackoutEndHour: 7,
};

export type RuleCheck = [passed: boolean, reason: string | null];

export class GuardrailRules {
  constructor(public readonly settings: GuardrailSettings = DEFAULT_GUARDRAIL_SETTINGS) {}

  checkHardDeclineBlocklist(failureClassStr: string): RuleCheck {
    const fc = toFailureClass(failureClassStr);
    if (fc === null) return [true, null];
    if (isHardDecline(fc)) {
      return [false, `Hard decline blocklist: ${fc} is non-retryable`];
    }
    return [true, null];
  }

  checkMaxRetriesPerPayment(paymentId: string, currentAttempts: number): RuleCheck {
    const limit = this.settings.maxRetriesPerPayment;
    if (currentAttempts >= limit) {
      return [
        false,
        `Max retries per payment exceeded: ${currentAttempts} >= ${limit} for ${paymentId}`,
      ];
    }
    return [true, null];
  }

  checkMaxRetriesPerCustomer(customerRetries24h: number): RuleCheck {
    const limit = this.settings.maxRetriesPerCustomer24h;
    if (customerRetries24h >= limit) {
      return [
        false,
        `Max retries per customer (24h) exceeded: ${customerRetries24h} >= ${limit}`,
      ];
    }
    return [true, null];
  }

  checkAmountCeiling(amountPaise: number): RuleCheck {
    const ceiling = this.settings.amountCeilingPaise;
    if (amountPaise > ceiling) {
      return [
        false,
        `Amount ceiling exceeded: ₹${(amountPaise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })} > ₹${(ceiling / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })}`,
      ];
    }
    return [true, null];
  }

  checkConsentWindow(failedAt: Date, currentTime: Date): RuleCheck {
    const windowHours = this.settings.consentWindowHours;
    const deadline = failedAt.getTime() + windowHours * 3_600_000;
    if (currentTime.getTime() > deadline) {
      const hoursElapsed = (currentTime.getTime() - failedAt.getTime()) / 3_600_000;
      return [
        false,
        `Consent window expired: ${hoursElapsed.toFixed(1)}h > ${windowHours}h`,
      ];
    }
    return [true, null];
  }

  checkCustomerNudgeRateLimit(nudges24h: number): RuleCheck {
    const limit = this.settings.maxNudgesPerCustomer24h;
    if (nudges24h >= limit) {
      return [false, `Nudge rate limit exceeded: ${nudges24h} >= ${limit} nudges in 24h`];
    }
    return [true, null];
  }

  checkTimeOfDayBlackout(currentHour: number): RuleCheck {
    if (isInBlackout(currentHour, this.settings)) {
      return [
        false,
        `Time-of-day blackout: hour ${currentHour} is within ${String(this.settings.retryBlackoutStartHour).padStart(2, "0")}:00-${String(this.settings.retryBlackoutEndHour).padStart(2, "0")}:00 IST`,
      ];
    }
    return [true, null];
  }

  checkIdempotencyKey(idempotencyKey: string | null | undefined): RuleCheck {
    if (!idempotencyKey || !idempotencyKey.trim()) {
      return [false, "Missing idempotency key — every retry must be idempotent"];
    }
    return [true, null];
  }
}
