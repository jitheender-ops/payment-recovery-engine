import { ClassifierMapper } from "./classifier.js";
import { GuardrailGate } from "./gate.js";
import { resolveTargetRail } from "./railSelector.js";
import { clampRetryAtOutOfBlackout, istHour } from "./time.js";
import {
  attachAttempt,
  effectiveCounts,
  fallbackAction,
  idempotencyKey,
  predictHeuristic,
  type CaseMutable,
} from "./policy.js";
import type { FailureContext, RetryAction } from "./actions.js";
import { FailureClass } from "./taxonomy.js";

const now = new Date("2026-08-26T14:10:00Z");
const paymentId = "pay_Qm9vU2hlbmFu";

console.log("── Payment Failure Recovery Engine — TypeScript pipeline demo ──");
console.log(`now: ${now.toISOString()} (IST hour ${istHour(now)})\n`);

const webhookEntity = {
  id: paymentId,
  order_id: "order_S4mPl3",
  amount: 249900,
  method: "card",
  bank: "HDFC",
  error_code: "BAD_REQUEST_ERROR",
  error_source: "customer",
  error_step: "payment_authentication",
  error_reason: "otp_timeout",
  email: "customer@example.com",
};

const classifier = new ClassifierMapper();
const { failureClass, retryable } = classifier.classify(
  webhookEntity.error_code,
  undefined,
  webhookEntity.error_source,
  webhookEntity.error_step,
  webhookEntity.error_reason,
);
console.log(`1. classify      → ${failureClass} (retryable=${retryable})`);

const ledgerRow = {
  totalRetries24h: 2,
  totalNudges24h: 1,
  lastRetryAt: new Date(now.getTime() - 5 * 3_600_000),
  lastNudgeAt: new Date(now.getTime() - 30 * 3_600_000),
};
const counts = effectiveCounts(ledgerRow, now, 24);
console.log(`2. ledger        → rolling counts: retries=${counts.retries}/24h nudges=${counts.nudges}/24h`);

const context: FailureContext = {
  paymentId,
  orderId: webhookEntity.order_id,
  failureClass,
  errorCode: webhookEntity.error_code,
  errorSource: webhookEntity.error_source,
  errorReason: webhookEntity.error_reason,
  amount: webhookEntity.amount,
  currency: "INR",
  method: webhookEntity.method,
  bank: webhookEntity.bank,
  customerId: webhookEntity.email,
  customerEmail: webhookEntity.email,
  retryCount24h: counts.retries,
  nudgeCount24h: counts.nudges,
  previousRetryOutcomes: [],
  failedAt: new Date(now.getTime() - 20 * 60_000),
  currentTime: now,
  hourOfDay: istHour(now),
  dayOfWeek: 2,
  isRetryable: retryable,
  originalFailureId: "11111111-1111-1111-1111-111111111111",
};

const llmAnswer: RetryAction = {
  action: "switch_rail",
  rail: "card",
  reason: "OTP dropoff on card; agent proposes switching to card again",
  confidence: 0.6,
};
console.log(`3. agent         → ${llmAnswer.action}/${llmAnswer.rail} (LLM unavailable in demo; treating answer as candidate)`);

let action: RetryAction;
if (llmAnswer.rail !== undefined && llmAnswer.rail === webhookEntity.method) {
  const resolved = resolveTargetRail(webhookEntity.method, llmAnswer.rail, failureClass);
  console.log(`4. rail resolve  → agent proposed the rail that just failed (${llmAnswer.rail}); override → ${resolved}`);
  action = { ...llmAnswer, rail: resolved };
} else {
  console.log(`4. rail resolve  → kept agent's rail`);
  action = llmAnswer;
}

action.retryAt = clampRetryAtOutOfBlackout(new Date(now.getTime() + 30 * 60_000), {
  retryBlackoutStartHour: 23,
  retryBlackoutEndHour: 7,
});
console.log(`5. blackout clamp→ retry_at ${action.retryAt.toISOString()} (IST hour ${istHour(action.retryAt)})`);

const attemptCount = 0;
const idemKey = idempotencyKey(paymentId, attemptCount);
console.log(`6. idempotency   → ${idemKey}`);

const gate = new GuardrailGate();
const guardrail = gate.validate(action, context, idemKey, attemptCount, now);
console.log(
  `7. guardrail     → passed=${guardrail.passed} rules=${guardrail.rulesChecked} failed=${guardrail.rulesFailed}${guardrail.rejectionReasons.length ? " " + JSON.stringify(guardrail.rejectionReasons) : ""}`,
);

const kase: CaseMutable = { state: "open", attemptsUsed: 0, maxAttempts: 3, escalationLevel: 0, nextActionAt: null };
attachAttempt(kase, { actionType: action.action }, 24, now);
console.log(`8. case          → attempts_used=${kase.attemptsUsed}/${kase.maxAttempts} escalation=${kase.escalationLevel} next_action_at=${kase.nextActionAt?.toISOString() ?? "null"}`);

console.log(`9. execute       → would create Razorpay Payment Link (write-ahead 'pending' committed first)`);
console.log(`\nLLM-degraded fallback for the same context would be: ${JSON.stringify(fallbackAction(context, "LLM error: HTTP 402"))}`);
console.log(`Heuristic (XGBoost-less) decision would be: ${JSON.stringify(predictHeuristic({ ...context, failureClass: FailureClass.ThreedsDropoff }))}`);
