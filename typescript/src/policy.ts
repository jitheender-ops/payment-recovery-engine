import { createHash } from "node:crypto";
import type { FailureContext, RetryAction } from "./actions.js";
import { TERMINAL_CASE_STATES as _TERMINAL } from "./entities.js";
import {
  FailureClass,
  isHardDecline,
  isRetryable,
  toFailureClass,
} from "./taxonomy.js";

export function idempotencyKey(paymentId: string, attemptCount: number): string {
  return `retry_${paymentId}_${attemptCount}`;
}

export interface LedgerCounters {
  totalRetries24h: number | null;
  totalNudges24h: number | null;
  lastRetryAt: Date | null;
  lastNudgeAt: Date | null;
}

export interface EffectiveCounts {
  retries: number;
  nudges: number;
}

export function effectiveCounts(
  ledger: LedgerCounters,
  now: Date,
  rateLimitWindowHours: number,
): EffectiveCounts {
  const windowMs = rateLimitWindowHours * 3_600_000;
  let retries = ledger.totalRetries24h ?? 0;
  let nudges = ledger.totalNudges24h ?? 0;
  if (ledger.lastRetryAt === null || now.getTime() - ledger.lastRetryAt.getTime() > windowMs) {
    retries = 0;
  }
  if (ledger.lastNudgeAt === null || now.getTime() - ledger.lastNudgeAt.getTime() > windowMs) {
    nudges = 0;
  }
  return { retries, nudges };
}

export const TERMINAL_STATES = _TERMINAL;

export interface CaseLike {
  state: string;
  attemptsUsed: number;
  maxAttempts: number;
  closeReason?: string | null;
  nextActionAt?: Date | null;
}

export interface LedgerConsent {
  consentStatus?: string | null;
}

export function stopReason(
  kase: CaseLike,
  ledger: LedgerConsent | null,
  now: Date = new Date(),
): string | null {
  if (_TERMINAL.has(kase.state)) {
    return `case already ${kase.state}: ${kase.closeReason ?? "no reason recorded"}`;
  }
  if (kase.attemptsUsed >= kase.maxAttempts) {
    return `attempt budget spent (${kase.attemptsUsed}/${kase.maxAttempts})`;
  }
  if (ledger !== null && ledger.consentStatus === "opted_out") {
    return "customer opted out of contact";
  }
  if (kase.nextActionAt != null && now.getTime() < kase.nextActionAt.getTime()) {
    return `next action not due until ${kase.nextActionAt.toISOString()}`;
  }
  return null;
}

export interface AttemptLike {
  actionType: string;
}

export interface CaseMutable extends CaseLike {
  escalationLevel: number;
}

export function attachAttempt(
  kase: CaseMutable,
  attempt: AttemptLike,
  escalationBackoffHours: number,
  now: Date = new Date(),
): void {
  kase.attemptsUsed += 1;
  if (attempt.actionType === "nudge_customer") {
    kase.escalationLevel += 1;
    kase.nextActionAt = new Date(
      now.getTime() + escalationBackoffHours * 3_600_000 * kase.escalationLevel,
    );
  }
}

export function extractFeatures(context: FailureContext): number[] {
  const features: number[] = [];

  for (const fc of Object.values(FailureClass)) {
    features.push(fc === context.failureClass ? 1.0 : 0.0);
  }

  for (const m of ["upi", "card", "netbanking", "wallet"]) {
    features.push(m === context.method ? 1.0 : 0.0);
  }

  features.push(context.hourOfDay / 23.0);
  features.push(context.dayOfWeek / 6.0);

  const amountLog =
    Math.log1p(Math.min(context.amount, 10_000_000)) / Math.log1p(10_000_000);
  features.push(amountLog);

  features.push(Math.min(context.retryCount24h, 10) / 10.0);
  features.push(context.isRetryable ? 1.0 : 0.0);

  const bankStr = context.bank ?? "unknown";
  const bankHash =
    parseInt(
      createHash("md5").update(bankStr, "utf8").digest("hex").slice(0, 8),
      16,
    ) / 0xffffffff;
  features.push(bankHash);

  return features;
}

export function predictHeuristic(context: FailureContext): RetryAction {
  const fc = toFailureClass(context.failureClass) ?? FailureClass.Unknown;

  if (isHardDecline(fc)) {
    return {
      action: "abandon",
      reason: `Rule-based: ${fc} is a hard decline`,
      confidence: 0.95,
    };
  }

  if (fc === FailureClass.NetworkError) {
    return {
      action: "retry_now",
      reason: "Rule-based: transient network error, immediate retry",
      confidence: 0.8,
    };
  }

  if (fc === FailureClass.PaymentTimeout) {
    return {
      action: "retry_now",
      reason: "Rule-based: payment timed out, immediate retry",
      confidence: 0.75,
    };
  }

  if (fc === FailureClass.BankDowntime) {
    return {
      action: "retry_at",
      retryAt: new Date(context.currentTime.getTime() + 30 * 60_000),
      reason: "Rule-based: bank downtime, retry in 30 minutes",
      confidence: 0.7,
    };
  }

  if (fc === FailureClass.ThreedsDropoff) {
    return {
      action: "switch_rail",
      rail: context.method !== "upi" ? "upi" : "card",
      reason: "Rule-based: 3DS drop-off, switch to simpler auth flow",
      confidence: 0.7,
    };
  }

  if (fc === FailureClass.IssuerDecline) {
    return {
      action: "switch_rail",
      rail: context.method !== "upi" ? "upi" : "netbanking",
      reason: "Rule-based: issuer decline, try different rail",
      confidence: 0.5,
    };
  }

  if (fc === FailureClass.InsufficientFunds) {
    return {
      action: "nudge_customer",
      reason: "Rule-based: insufficient funds, customer needs to act",
      confidence: 0.7,
    };
  }

  if (fc === FailureClass.UpiCollectTimeout) {
    return {
      action: "nudge_customer",
      reason: "Rule-based: UPI collect timeout, remind customer to approve",
      confidence: 0.65,
    };
  }

  if (fc === FailureClass.CardLimitExceeded) {
    return {
      action: "nudge_customer",
      reason: "Rule-based: card limit exceeded, suggest alternate method",
      confidence: 0.65,
    };
  }

  if (isRetryable(fc)) {
    return {
      action: "retry_at",
      retryAt: new Date(context.currentTime.getTime() + 15 * 60_000),
      reason: `Rule-based: ${fc}, conservative retry in 15 min`,
      confidence: 0.4,
    };
  }

  return {
    action: "abandon",
    reason: `Rule-based: ${fc}, non-retryable, abandon`,
    confidence: 0.5,
  };
}

export function fallbackAction(context: FailureContext, errorDetail: string): RetryAction {
  const fc = toFailureClass(context.failureClass) ?? FailureClass.Unknown;

  if (isHardDecline(fc)) {
    return {
      action: "abandon",
      reason: `Fallback: hard decline (${errorDetail})`,
      confidence: 0.9,
    };
  }

  if (fc === FailureClass.NetworkError) {
    return {
      action: "retry_now",
      reason: `Fallback: network error, immediate retry (${errorDetail})`,
      confidence: 0.6,
    };
  }

  if (fc === FailureClass.BankDowntime) {
    return {
      action: "retry_at",
      retryAt: new Date(context.currentTime.getTime() + 30 * 60_000),
      reason: `Fallback: bank downtime, retry in 30min (${errorDetail})`,
      confidence: 0.5,
    };
  }

  return {
    action: "abandon",
    reason: `Fallback: conservative abandon (${errorDetail})`,
    confidence: 0.3,
  };
}
