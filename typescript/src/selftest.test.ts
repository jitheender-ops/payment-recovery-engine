import assert from "node:assert/strict";
import test from "node:test";
import { ClassifierMapper } from "./classifier.js";
import { GuardrailGate } from "./gate.js";
import { validateActionSchema } from "./schemas.js";
import { resolveTargetRail } from "./railSelector.js";
import { clampRetryAtOutOfBlackout, istHour } from "./time.js";
import { DEFAULT_GUARDRAIL_SETTINGS } from "./rules.js";
import {
  attachAttempt,
  effectiveCounts,
  extractFeatures,
  fallbackAction,
  idempotencyKey,
  predictHeuristic,
  stopReason,
  type CaseMutable,
} from "./policy.js";
import { parseRetryAction } from "./actions.js";
import { FailureClass } from "./taxonomy.js";
import type { FailureContext } from "./actions.js";

function ctx(overrides: Partial<FailureContext> = {}): FailureContext {
  const now = new Date("2026-08-26T10:00:00Z");
  return {
    paymentId: "pay_ABC123",
    failureClass: FailureClass.InsufficientFunds,
    errorCode: "BAD_REQUEST_ERROR",
    amount: 150000,
    currency: "INR",
    method: "card",
    retryCount24h: 0,
    nudgeCount24h: 0,
    previousRetryOutcomes: [],
    failedAt: new Date(now.getTime() - 3_600_000),
    currentTime: now,
    hourOfDay: istHour(now),
    dayOfWeek: 2,
    isRetryable: true,
    ...overrides,
  };
}

test("classifier: fraud reason beats lower-priority code fallbacks", () => {
  const mapper = new ClassifierMapper();
  const r = mapper.classify("GATEWAY_ERROR", "risk", "gateway", null, "payment_risk_check_failed");
  assert.equal(r.failureClass, FailureClass.FraudBlock);
  assert.equal(r.retryable, false);
});

test("classifier: step-qualified rule requires the step", () => {
  const mapper = new ClassifierMapper();
  const withStep = mapper.classify("BAD_REQUEST_ERROR", null, "customer", "payment_authentication", "invalid_otp");
  assert.equal(withStep.failureClass, FailureClass.ThreedsDropoff);
  const withoutStep = mapper.classify("BAD_REQUEST_ERROR", null, "customer", null, "invalid_otp");
  assert.equal(withoutStep.failureClass, FailureClass.IssuerDecline);
});

test("classifier: unknown falls back non-retryable", () => {
  const mapper = new ClassifierMapper();
  const r = mapper.classify("SOMETHING_ELSE", null, "nowhere", null, null);
  assert.equal(r.failureClass, FailureClass.Unknown);
  assert.equal(r.retryable, false);
});

test("retry action parser enforces the constrained space", () => {
  assert.equal(parseRetryAction({ action: "fly_to_moon", reason: "because" }).ok, false);
  assert.equal(parseRetryAction({ action: "retry_now", reason: "no" }).ok, false);
  assert.equal(parseRetryAction({ action: "retry_now", reason: "valid reason", confidence: 1.5 }).ok, false);
  const ok = parseRetryAction({ action: "switch_rail", rail: "upi", reason: "issuer keeps declining card" });
  assert.ok(ok.ok);
});

test("schema: switch_rail requires rail; retry_at requires future timestamp", () => {
  const now = new Date("2026-08-26T10:00:00Z");
  const missingRail = validateActionSchema({ action: "switch_rail", reason: "switch the rail please" }, now);
  assert.equal(missingRail.valid, false);

  const past = validateActionSchema(
    { action: "retry_at", retryAt: new Date("2026-08-26T09:00:00Z"), reason: "too late to matter" },
    now,
  );
  assert.equal(past.valid, false);

  const good = validateActionSchema(
    { action: "retry_at", retryAt: new Date("2026-08-26T11:00:00Z"), reason: "wait an hour" },
    now,
  );
  assert.equal(good.valid, true);
});

test("guardrail collects ALL violations, no short-circuit", () => {
  const gate = new GuardrailGate();
  const bad = ctx({
    amount: 6_000_000,
    retryCount24h: 5,
    hourOfDay: 1,
  });
  const result = gate.validate(
    { action: "retry_now", reason: "try again immediately" },
    bad,
    idempotencyKey("pay_ABC123", 3),
    3,
    bad.currentTime,
  );
  assert.equal(result.passed, false);
  assert.equal(result.rulesFailed, result.rejectionReasons.length);
  assert.ok(result.rejectionReasons.length >= 4);
  assert.ok(result.rejectionReasons.some((r) => r.startsWith("Amount ceiling")));
  assert.ok(result.rejectionReasons.some((r) => r.startsWith("Max retries per payment")));
  assert.ok(result.rejectionReasons.some((r) => r.startsWith("Max retries per customer")));
  assert.ok(result.rejectionReasons.some((r) => r.startsWith("Time-of-day blackout")));
});

test("guardrail: abandon always passes", () => {
  const gate = new GuardrailGate();
  const result = gate.validate(
    { action: "abandon", reason: "not worth chasing" },
    ctx({ hourOfDay: 1, amount: 9_999_999 }),
    "",
    99,
    new Date(),
  );
  assert.equal(result.passed, true);
  assert.equal(result.rulesChecked, 0);
});

test("blackout clamp: 23:05 IST deferral moves to 07:05 IST next day, forward-only", () => {
  const retryAt = new Date("2026-08-26T17:35:00Z");
  assert.equal(istHour(retryAt), 23);
  const clamped = clampRetryAtOutOfBlackout(retryAt, DEFAULT_GUARDRAIL_SETTINGS);
  assert.equal(clamped.toISOString(), "2026-08-27T01:35:00.000Z");
  assert.equal(istHour(clamped), 7);
  assert.ok(clamped.getTime() > retryAt.getTime());
});

test("blackout clamp: daytime deferral untouched", () => {
  const retryAt = new Date("2026-08-26T08:30:00Z");
  assert.equal(clampRetryAtOutOfBlackout(retryAt, DEFAULT_GUARDRAIL_SETTINGS), retryAt);
});

test("rolling 24h window: stale tallies read as zero", () => {
  const now = new Date("2026-08-26T10:00:00Z");
  const fresh = effectiveCounts(
    { totalRetries24h: 5, totalNudges24h: 2, lastRetryAt: new Date(now.getTime() - 2 * 3_600_000), lastNudgeAt: new Date(now.getTime() - 1 * 3_600_000) },
    now,
    24,
  );
  assert.deepEqual(fresh, { retries: 5, nudges: 2 });

  const stale = effectiveCounts(
    { totalRetries24h: 5, totalNudges24h: 2, lastRetryAt: new Date(now.getTime() - 25 * 3_600_000), lastNudgeAt: null },
    now,
    24,
  );
  assert.deepEqual(stale, { retries: 0, nudges: 0 });
});

test("rail resolution: switching back to the just-failed rail is overridden", () => {
  assert.equal(resolveTargetRail("card", "card", FailureClass.IssuerDecline), "upi");
  assert.equal(resolveTargetRail("card", "upi", FailureClass.IssuerDecline), "upi");
  assert.equal(resolveTargetRail("upi", null, FailureClass.UpiCollectTimeout), "card");
});

test("idempotency key is deterministic", () => {
  assert.equal(idempotencyKey("pay_X", 0), "retry_pay_X_0");
  assert.equal(idempotencyKey("pay_X", 0), idempotencyKey("pay_X", 0));
});

test("stop reason ordering: terminal > budget > opt-out > defer", () => {
  const now = new Date("2026-08-26T10:00:00Z");
  assert.ok(stopReason({ state: "recovered", attemptsUsed: 0, maxAttempts: 3 }, null, now));
  assert.equal(
    stopReason({ state: "open", attemptsUsed: 3, maxAttempts: 3 }, { consentStatus: "opted_out" }, now),
    "attempt budget spent (3/3)",
  );
  assert.equal(
    stopReason({ state: "open", attemptsUsed: 1, maxAttempts: 3 }, { consentStatus: "opted_out" }, now),
    "customer opted out of contact",
  );
  assert.equal(
    stopReason(
      { state: "open", attemptsUsed: 1, maxAttempts: 3, nextActionAt: new Date(now.getTime() + 3_600_000) },
      { consentStatus: "granted" },
      now,
    ),
    `next action not due until ${new Date(now.getTime() + 3_600_000).toISOString()}`,
  );
  assert.equal(
    stopReason({ state: "open", attemptsUsed: 1, maxAttempts: 3 }, { consentStatus: "granted" }, now),
    null,
  );
});

test("attachAttempt: nudges escalate and buy widening quiet time", () => {
  const now = new Date("2026-08-26T10:00:00Z");
  const kase: CaseMutable = { state: "open", attemptsUsed: 0, maxAttempts: 3, escalationLevel: 0, nextActionAt: null };
  attachAttempt(kase, { actionType: "nudge_customer" }, 24, now);
  assert.equal(kase.attemptsUsed, 1);
  assert.equal(kase.escalationLevel, 1);
  assert.equal(kase.nextActionAt?.toISOString(), new Date(now.getTime() + 24 * 3_600_000).toISOString());
  attachAttempt(kase, { actionType: "nudge_customer" }, 24, now);
  assert.equal(kase.nextActionAt?.toISOString(), new Date(now.getTime() + 48 * 3_600_000).toISOString());
  const quiet: CaseMutable = { state: "open", attemptsUsed: 0, maxAttempts: 3, escalationLevel: 0, nextActionAt: null };
  attachAttempt(quiet, { actionType: "switch_rail" }, 24, now);
  assert.equal(quiet.nextActionAt, null);
  assert.equal(quiet.escalationLevel, 0);
});

test("heuristic policy mirrors the class table", () => {
  const out = predictHeuristic(ctx({ failureClass: FailureClass.InsufficientFunds }));
  assert.equal(out.action, "nudge_customer");
  const down = predictHeuristic(ctx({ failureClass: FailureClass.BankDowntime }));
  assert.equal(down.action, "retry_at");
  const hard = predictHeuristic(ctx({ failureClass: FailureClass.FraudBlock }));
  assert.equal(hard.action, "abandon");
});

test("llm fallback degrades conservatively", () => {
  const out = fallbackAction(ctx({ failureClass: FailureClass.InsufficientFunds }), "LLM error: timeout");
  assert.equal(out.action, "abandon");
  assert.ok(out.reason?.includes("LLM error: timeout"));
});

test("feature vector matches the Python implementation (24 dims; the py docstring's 23 is off by one)", () => {
  assert.equal(predictHeuristic(ctx()).confidence !== undefined, true);
  assert.equal(extractFeatures(ctx()).length, 24);
});
