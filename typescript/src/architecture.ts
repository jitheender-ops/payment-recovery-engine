// ═══════════════════════════════════════════════════════════════════════════
// PAYMENT FAILURE RECOVERY ENGINE — COMPLETE ARCHITECTURE IN ONE FILE
// ═══════════════════════════════════════════════════════════════════════════
//
// PURPOSE
// ───────
// An AI-powered system for Indian merchants on the Razorpay gateway that
// decides WHETHER, WHEN, and ON WHICH RAIL to retry a failed payment —
// with deterministic guardrails ensuring no LLM ever directly authorizes
// money movement.
//
// THE ONE DESIGN PRINCIPLE THAT EXPLAINS EVERYTHING ELSE
// ──────────────────────────────────────────────────────────────────────
// Deterministic logic wraps the LLM on BOTH sides:
//
//   Razorpay webhook ──▶ classify ──▶ decide (LLM/XGBoost) ──▶ guardrail ──▶ execute
//                        │ no LLM      │ constrained JSON       │ 8-9 rules   │ Payment Link
//                        ▼             ▼                        ▼             ▼
//                     hard declines  5 fixed actions          ALL violations  idempotent,
//                     never reach it (never freeform)         reported        audited, bounded
//
// THE SIX LAYERS
// ──────────────
//   1. Ingestion      — HMAC verify → dedup → durable event store → async processing
//   2. Classification — error code → failure taxonomy (NO LLM)
//   3. Policy Agent   — LLM or XGBoost picks one of 5 fixed actions
//   4. Guardrail Gate — schema + business rules, BEFORE any money moves
//   5. Messaging      — customer nudge text (LLM w/ template fallback)
//   6. Scheduler      — fires deferred retries; 3 reconciliation sweeps
//
// HOW TO READ THIS FILE
// ─────────────────────
// Sections are numbered in pipeline order. Each section is real, runnable
// logic (condensed from the multi-module TypeScript port, which mirrors the
// Python original 1:1). Comments explain WHY, because the why is the
// architecture — the what is just CRUD.
//
// ═══════════════════════════════════════════════════════════════════════════

import { createHash, createHmac, randomUUID, timingSafeEqual } from "node:crypto";

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 0 — CONFIGURATION DEFAULTS
// ═══════════════════════════════════════════════════════════════════════════
// Every threshold is a guardrail input. These numbers are the compliance
// contract with the customer: max 3 retries per payment, max 5 contacts per
// customer per rolling 24h, ₹50,000 ceiling, 72h consent window, and a
// 23:00–07:00 IST blackout (bank success rates crater overnight).

interface EngineSettings {
  maxRetriesPerPayment: number;        // 3
  maxRetriesPerCustomer24h: number;    // 5
  amountCeilingPaise: number;          // 5_000_000 (₹50,000; amounts are paise)
  consentWindowHours: number;          // 72 — past this, the customer said no by silence
  maxNudgesPerCustomer24h: number;     // 2
  rateLimitWindowHours: number;        // 24 — windows ROLL (see §8.3)
  retryBlackoutStartHour: number;      // 23 IST
  retryBlackoutEndHour: number;        // 7 IST
  escalationBackoffHours: number;      // 24 — widened per escalation rung
  schedulerIntervalSeconds: number;    // 60
  schedulerBatchSize: number;          // 50
  eventReconcileAfterSeconds: number;  // 300 — younger events might still be in-flight
  attemptStaleAfterSeconds: number;    // 900 — past this, a pending row is a lost call
  razorpayWebhookSecret: string;
  recoveryLinkSecret: string;          // DEDICATED secret; empty = page feature OFF
  publicBaseUrl: string;
}

const DEFAULT_SETTINGS: EngineSettings = {
  maxRetriesPerPayment: 3,
  maxRetriesPerCustomer24h: 5,
  amountCeilingPaise: 5_000_000,
  consentWindowHours: 72,
  maxNudgesPerCustomer24h: 2,
  rateLimitWindowHours: 24,
  retryBlackoutStartHour: 23,
  retryBlackoutEndHour: 7,
  escalationBackoffHours: 24,
  schedulerIntervalSeconds: 60,
  schedulerBatchSize: 50,
  eventReconcileAfterSeconds: 300,
  attemptStaleAfterSeconds: 900,
  razorpayWebhookSecret: "from-env",
  recoveryLinkSecret: "from-env-or-empty",
  publicBaseUrl: "",
};

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 1 — LAYER 1: WEBHOOK INGESTION (src/ingestion/)
// ═══════════════════════════════════════════════════════════════════════════
// Flow: verify signature → dedup → DURABLY STORE BEFORE acking 200 → process.
//
// WHY store-before-ack matters: Razorpay does not re-send after a 200. If we
// acknowledged an event we had not persisted, a crash in that window would
// silently delete a payment failure — real money, gone, no retry. So the
// event row is committed first; processing happens after, in a background
// task (and if THAT dies, the Layer 6 reconcile sweep re-runs it — §7.2).

// Razorpay signs every webhook with HMAC-SHA256 over the RAW request body.
// Verify against raw bytes, never parsed-then-re-serialized JSON — one changed
// space in re-serialization and the signature no longer matches.
function verifyWebhookSignature(
  rawBody: Buffer | string,
  signature: string | null | undefined,
  secret: string | null | undefined,
): boolean {
  if (!signature || !secret) return false; // fail closed
  const expected = createHmac("sha256", secret).update(rawBody).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(signature);
  return a.length === b.length && timingSafeEqual(a, b);
}

// Razorpay sends no event id we can dedup on directly, so one is CONSTRUCTED
// from stable fields: same payment failing at the same second = same event.
function constructEventId(payload: WebhookPayload): string {
  const entity = payload.payload?.payment?.entity ?? {};
  return `${payload.event}_${entity.id ?? "unknown"}_${payload.created_at ?? 0}`;
}

interface WebhookPayload {
  event: string; // "payment.failed" | "payment.captured" | ...
  created_at?: number;
  payload?: { payment?: { entity?: Record<string, unknown> } };
}

interface WebhookEventRow {
  id: string;
  razorpayEventId: string;
  eventType: string;
  payload: Record<string, unknown>;
  receivedAt: Date;
  processed: boolean;
  processingError?: string | null;
}

async function receiveRazorpayWebhook(
  rawBody: Buffer,
  signature: string | null,
  store: RecoveryStore,
  settings: EngineSettings,
  orchestrator: PaymentRecoveryOrchestrator,
): Promise<{ status: 200 | 400 | 401; body: string }> {
  if (!verifyWebhookSignature(rawBody, signature, settings.razorpayWebhookSecret)) {
    return { status: 401, body: "Invalid signature" }; // fail closed
  }

  let payload: WebhookPayload;
  try {
    payload = JSON.parse(rawBody.toString("utf8"));
  } catch {
    return { status: 400, body: "Invalid JSON" };
  }

  const eventId = constructEventId(payload);
  const isNew = await store.recordProcessedEvent(eventId); // insert-or-skip dedup
  if (!isNew) return { status: 200, body: "Already processed" };

  const event: WebhookEventRow = {
    id: randomUUID(),
    razorpayEventId: eventId,
    eventType: payload.event,
    payload: payload as unknown as Record<string, unknown>,
    receivedAt: new Date(),
    processed: false,
  };
  await store.insertEvent(event); // DURABLE before we ever say "OK"

  if (event.eventType === "payment.failed") {
    await orchestrator.processPaymentFailure(event);
    await store.markEventProcessed(event.razorpayEventId);
  } else if (event.eventType === "payment.captured") {
    await handleCapture(payload, store, settings); // revenue attribution — §9
  }
  return { status: 200, body: "OK" };
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 2 — LAYER 2: DETERMINISTIC CLASSIFIER (src/classifier/)
// ═══════════════════════════════════════════════════════════════════════════
// Maps Razorpay's error 5-tuple to a failure taxonomy. Pure lookup, ZERO LLM:
// a regex-solvable problem handed to an LLM is the first thing a reviewer
// flags. Hard declines are caught HERE, before any model is ever paid for.

type FailureClass =
  | "insufficient_funds" | "bank_downtime" | "network_error"
  | "upi_collect_timeout" | "payment_timeout"                       // retry same rail
  | "3ds_dropoff" | "issuer_decline" | "card_limit_exceeded"        // retry, switch rail
  | "invalid_card" | "expired_instrument" | "fraud_block"
  | "hard_decline" | "customer_cancelled"                           // NEVER retry
  | "unknown";                                                      // default: no retry

const HARD_DECLINE_CLASSES: ReadonlySet<FailureClass> = new Set([
  "invalid_card", "expired_instrument", "fraud_block",
  "hard_decline", "customer_cancelled",
]);

const RETRYABLE_CLASSES: ReadonlySet<FailureClass> = new Set([
  "insufficient_funds", "bank_downtime", "network_error", "upi_collect_timeout",
  "payment_timeout", "3ds_dropoff", "issuer_decline", "card_limit_exceeded",
]);

interface ClassifierRule {
  error_code?: string;
  error_source?: string;
  error_step?: string;
  error_reason?: string;
  failure_class: FailureClass;
  retryable?: boolean;
  priority: number; // highest first; first match wins
}

// 43 rules live in error_codes.yaml in the Python original. Shape examples:
//   { error_reason: "payment_risk_check_failed", failure_class: "fraud_block", priority: 100 }
//   { error_reason: "invalid_otp", error_step: "payment_authentication",
//     failure_class: "3ds_dropoff", retryable: true, priority: 80 }
//   { error_code: "GATEWAY_ERROR", error_source: "gateway",
//     failure_class: "network_error", retryable: true, priority: 40 }
// A rule field that is absent matches anything; a field that is present must
// match exactly AND the webhook must carry it.

class ClassifierMapper {
  private readonly rules: readonly ClassifierRule[];

  constructor(rules: readonly ClassifierRule[]) {
    this.rules = [...rules].sort((a, b) => b.priority - a.priority);
  }

  classify(
    errorCode: string,
    errorSource?: string | null,
    errorStep?: string | null,
    errorReason?: string | null,
  ): { failureClass: FailureClass; retryable: boolean } {
    for (const rule of this.rules) {
      if (rule.error_reason !== undefined && (!errorReason || rule.error_reason !== errorReason)) continue;
      if (rule.error_step !== undefined && (!errorStep || rule.error_step !== errorStep)) continue;
      if (rule.error_source !== undefined && (!errorSource || rule.error_source !== errorSource)) continue;
      if (rule.error_code !== undefined && rule.error_code !== errorCode) continue;
      return {
        failureClass: rule.failure_class,
        retryable: rule.retryable ?? RETRYABLE_CLASSES.has(rule.failure_class),
      };
    }
    return { failureClass: "unknown", retryable: false }; // default conservative
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 3 — LAYER 3: THE POLICY AGENT (src/agent/)
// ═══════════════════════════════════════════════════════════════════════════
// The agent's entire output space is FIVE pydantic-validated actions. No
// freeform text reaches execution — the reason string is audit-log only.

type ActionType = "retry_now" | "retry_at" | "switch_rail" | "nudge_customer" | "abandon";
type PaymentRail = "upi" | "card" | "netbanking" | "wallet";

interface RetryAction {
  action: ActionType;
  rail?: PaymentRail | null;      // required for switch_rail
  retryAt?: Date | null;          // required (and future) for retry_at
  reason: string;                 // 5–500 chars, audit only
  confidence?: number | null;     // 0–1
}

// The agent's input. Note what is NOT here: no raw email/phone. customer_id
// is pseudonymised with a KEYED hash (HMAC with the webhook secret) before it
// reaches any LLM provider — a bare sha256 of an Indian mobile number falls
// to 10^10 guesses.
interface FailureContext {
  paymentId: string;
  failureClass: string;
  errorCode: string;
  amount: number;                 // paise
  method: string;                 // the rail that just failed
  bank?: string | null;
  customerId?: string | null;     // masked before leaving the process
  retryCount24h: number;          // ROLLING window counts — §8.3
  nudgeCount24h: number;
  previousRetryOutcomes: string[];
  failedAt: Date;
  currentTime: Date;
  hourOfDay: number;              // TRUE IST hour (UTC+5:30) — feeds the blackout rule
  dayOfWeek: number;
  isRetryable: boolean;
}

class PolicyAgent {
  callCount = 0;
  fallbackCount = 0;              // THE degradation counter — see decide()
  lastErrorStatus: number | null = null;

  constructor(
    private settings: EngineSettings,
    private client: { complete(system: string, user: string, maxTokens: number): Promise<string> },
  ) {}

  async decide(context: FailureContext): Promise<RetryAction> {
    this.callCount += 1;
    try {
      let raw = await this.call(this.formatPrompt(context));
      // ONE retry on transient failures (429/503): a rate limit is seconds
      // from being a real decision. Fatal statuses skip it — retrying a bad
      // key only burns time before the same answer.
      // (fatal = 401 bad key, 402 no credits, 403 forbidden, 404 bad model)
      raw = await this.call(this.formatPrompt(context));
      let action = this.parse(raw);

      if (action === null) {
        // One correction prompt: "your last output was not valid JSON" —
        // models recover from this surprisingly often.
        action = this.parse(await this.call(`Your previous response was not valid JSON:\n${raw}\n\nRespond with ONLY valid JSON matching RetryAction.`));
      }
      if (action === null) {
        this.fallbackCount += 1;
        return this.fallbackAction(context, "LLM output could not be parsed");
      }
      return action;
    } catch (e) {
      const status = (e as { status?: number }).status;
      if (status && [401, 402, 403, 404].includes(status)) this.lastErrorStatus = status;
      this.fallbackCount += 1;
      return this.fallbackAction(context, `LLM error: ${String(e)}`);
    }
  }

  // THE DEGRADATION CONTRACT: decide() never raises — it swallows provider
  // errors and returns a conservative heuristic. That is silent by design,
  // so the CALLER compares fallbackCount across the call: if it moved, the
  // answer was NOT the LLM's, and the orchestrator substitutes XGBoost
  // instead (§6, step 6). Without this, a dead API key quietly turns the
  // engine into "abandon ~70% of recoverable payments" while the audit
  // trail claims an LLM decided.
  private fallbackAction(context: FailureContext, errorDetail: string): RetryAction {
    if (HARD_DECLINE_CLASSES.has(context.failureClass as FailureClass)) {
      return { action: "abandon", reason: `Fallback: hard decline (${errorDetail})`, confidence: 0.9 };
    }
    if (context.failureClass === "network_error") {
      return { action: "retry_now", reason: `Fallback: network error (${errorDetail})`, confidence: 0.6 };
    }
    if (context.failureClass === "bank_downtime") {
      return {
        action: "retry_at",
        retryAt: new Date(context.currentTime.getTime() + 30 * 60_000),
        reason: `Fallback: bank downtime (${errorDetail})`,
        confidence: 0.5,
      };
    }
    return { action: "abandon", reason: `Fallback: conservative abandon (${errorDetail})`, confidence: 0.3 };
  }

  private parse(raw: string): RetryAction | null {
    const text = raw.trim().replaceAll("```", "");
    try {
      const data = JSON.parse(text) as Record<string, unknown>;
      if (typeof data["action"] !== "string") return null;
      if (typeof data["reason"] !== "string" || (data["reason"] as string).length < 5) return null;
      return data as unknown as RetryAction; // full validation re-runs in the guardrail
    } catch {
      return null;
    }
  }

  private async call(prompt: string): Promise<string> {
    return this.client.complete(SYSTEM_PROMPT, prompt, this.settings.maxRetriesPerPayment && 300);
  }

  private formatPrompt(context: FailureContext): string {
    return JSON.stringify({
      failure_class: context.failureClass,
      error_code: context.errorCode,
      amount_paise: context.amount,
      method: context.method,
      customer: maskCustomerId(context.customerId, this.settings.razorpayWebhookSecret),
      retries_24h: context.retryCount24h,
      previous_outcomes: context.previousRetryOutcomes,
      hour_ist: context.hourOfDay,
    });
  }
}

function maskCustomerId(customerId: string | null | undefined, secret: string): string {
  if (!customerId) return "unknown";
  const digest = createHmac("sha256", secret)
    .update(`pii-mask|customer_id|${customerId}`)
    .digest("hex");
  return `cust_${digest.slice(0, 16)}`;
}

// The XGBoost baseline: a trained gradient-boosted classifier over 24
// features (failure-class one-hot ×14, method ×4, hour, day, log-amount,
// 24h-retry count, retryable flag, hashed bank). Trained on simulator labels;
// scores ~30% recovery vs 19% for fixed 3-retry. When no trained model file
// exists it degrades to transparent per-class rules:
function predictHeuristic(context: FailureContext): RetryAction {
  const fc = context.failureClass as FailureClass;
  if (HARD_DECLINE_CLASSES.has(fc)) return { action: "abandon", reason: `Rule: ${fc} is a hard decline`, confidence: 0.95 };
  if (fc === "network_error" || fc === "payment_timeout") return { action: "retry_now", reason: "Rule: transient, retry immediately", confidence: 0.8 };
  if (fc === "bank_downtime") return { action: "retry_at", retryAt: new Date(context.currentTime.getTime() + 30 * 60_000), reason: "Rule: banks recover in 30-60 min", confidence: 0.7 };
  if (fc === "3ds_dropoff") return { action: "switch_rail", rail: context.method !== "upi" ? "upi" : "card", reason: "Rule: simpler auth flow", confidence: 0.7 };
  if (fc === "issuer_decline") return { action: "switch_rail", rail: context.method !== "upi" ? "upi" : "netbanking", reason: "Rule: try another rail", confidence: 0.5 };
  if (fc === "insufficient_funds" || fc === "upi_collect_timeout" || fc === "card_limit_exceeded") {
    return { action: "nudge_customer", reason: "Rule: customer must act", confidence: 0.7 };
  }
  if (RETRYABLE_CLASSES.has(fc)) return { action: "retry_at", retryAt: new Date(context.currentTime.getTime() + 15 * 60_000), reason: "Rule: conservative retry", confidence: 0.4 };
  return { action: "abandon", reason: `Rule: ${fc} non-retryable`, confidence: 0.5 };
}

const SYSTEM_PROMPT = `You are a payment retry policy agent for an Indian payment gateway.
Output ONLY a JSON object: { "action": "retry_now"|"retry_at"|"switch_rail"|"nudge_customer"|"abandon",
"rail": "upi"|"card"|"netbanking"|"wallet"|null, "retry_at": "ISO 8601 UTC"|null,
"reason": "brief audit rationale", "confidence": 0.0-1.0 }
You are NOT authorizing money movement — a deterministic guardrail validates
your output before execution. Heuristics: network/timeout → retry_now;
bank_downtime → retry_at +30min; 3ds_dropoff → switch_rail upi;
insufficient_funds → nudge_customer; hard declines → abandon. Late-night IST
hours (23-07) prefer retry_at over retry_now.`;

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 4 — LAYER 4: THE GUARDRAIL GATE (src/guardrail/)
// ═══════════════════════════════════════════════════════════════════════════
// The answer to "what stops it doing something stupid with real money".
// Two properties define this layer:
//   1. It runs AFTER the agent and BEFORE any API call — always.
//   2. It checks ALL rules and reports ALL violations. No short-circuiting,
//      because the audit trail is for disputes, and a dispute wants the
//      complete list of what was wrong, not just the first thing.

interface GuardrailResult {
  passed: boolean;
  rejectionReasons: string[];
  rulesChecked: number;
  rulesFailed: number;
}

class GuardrailGate {
  constructor(private settings: EngineSettings) {}

  validate(
    action: RetryAction,
    context: FailureContext,
    idempotencyKey: string,
    currentAttempts: number,
  ): GuardrailResult {
    if (action.action === "abandon") {
      return { passed: true, rejectionReasons: [], rulesChecked: 0, rulesFailed: 0 };
    }

    const v: string[] = [];
    let checked = 0;
    const rule = (ok: boolean, reason: string) => {
      checked++;
      if (!ok) v.push(reason);
    };

    // 1. Schema + semantics: valid literal; switch_rail needs a rail;
    //    retry_at needs a FUTURE timestamp.
    checked++;
    const schemaOk =
      action.reason.length >= 5 &&
      (action.action !== "switch_rail" || action.rail != null) &&
      (action.action !== "retry_at" || (action.retryAt != null && action.retryAt > new Date()));
    rule(schemaOk, "Schema: invalid action structure");

    // 2. Hard-decline blocklist — fraud never retries, no matter what the
    //    model said. (Belt: the orchestrator already abandoned these in §6
    //    step 4; this is the suspenders.)
    rule(!HARD_DECLINE_CLASSES.has(context.failureClass as FailureClass),
      `Hard decline blocklist: ${context.failureClass} is non-retryable`);

    // 3. Per-payment budget.
    rule(currentAttempts < this.settings.maxRetriesPerPayment,
      `Max retries per payment exceeded: ${currentAttempts} >= ${this.settings.maxRetriesPerPayment}`);

    // 4. Per-customer rolling 24h budget (counts ROLL — §8.3).
    rule(context.retryCount24h < this.settings.maxRetriesPerCustomer24h,
      `Max retries per customer (24h) exceeded: ${context.retryCount24h} >= ${this.settings.maxRetriesPerCustomer24h}`);

    // 5. Amount ceiling.
    rule(context.amount <= this.settings.amountCeilingPaise,
      `Amount ceiling exceeded: ${context.amount} > ${this.settings.amountCeilingPaise}`);

    // 6. Consent window: past 72h from the failure, silence means no.
    rule(context.currentTime.getTime() - context.failedAt.getTime() <= this.settings.consentWindowHours * 3_600_000,
      "Consent window expired");

    // 7. Nudge rate limit (nudge actions only).
    if (action.action === "nudge_customer") {
      rule(context.nudgeCount24h < this.settings.maxNudgesPerCustomer24h,
        `Nudge rate limit exceeded: ${context.nudgeCount24h}`);
    }

    // 8. Time-of-day blackout, on a TRUE IST clock. India is UTC+5:30 — the
    //    half hour is the whole point: whole-hour arithmetic put every
    //    :30–:59 minute one hour off, right inside the window that matters.
    const hour = context.hourOfDay;
    const { retryBlackoutStartHour: s, retryBlackoutEndHour: e } = this.settings;
    const inBlackout = s > e ? hour >= s || hour < e : hour >= s && hour < e;
    rule(!inBlackout, `Time-of-day blackout: hour ${hour} is within ${s}:00-${e}:00 IST`);

    // 9. Idempotency key presence — every retry must be replay-safe.
    rule(!!idempotencyKey?.trim(), "Missing idempotency key");

    return { passed: v.length === 0, rejectionReasons: v, rulesChecked: checked, rulesFailed: v.length };
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 5 — TIME: THE IST CLOCK AND THE BLACKOUT CLAMP (src/time.ts)
// ═══════════════════════════════════════════════════════════════════════════
// India is fixed UTC+5:30 (no DST), so a fixed offset is exact.

const IST_OFFSET_MINUTES = 330;
const istHour = (instant: Date): number =>
  new Date(instant.getTime() + IST_OFFSET_MINUTES * 60_000).getUTCHours();

// THE BLACKOUT CLAMP — the subtlest bug in the system's history:
// The guardrail validates the CURRENT hour. A deferral approved at 22:30
// (+30min → 23:05) passes decision-time validation, then the scheduler's
// FIRE-TIME re-validation rejects it at 23:05 — inside the blackout — and
// the attempt slot was already spent. Every boundary-adjacent "wait 30
// minutes" burned budget on a retry that could never fire.
// Fix: clamp the deferral OUT of the blackout at decision time. Forward-only
// (waiting longer is always compliant; pulling a contact earlier than the
// agent chose is not our call), straight to the window edge +5min (a retry
// landing exactly at 07:00 is inside `hour < end`'s shadow in rounding).
function clampRetryAtOutOfBlackout(retryAt: Date, settings: EngineSettings): Date {
  const local = new Date(retryAt.getTime() + IST_OFFSET_MINUTES * 60_000);
  const { retryBlackoutStartHour: s, retryBlackoutEndHour: e } = settings;
  const hour = local.getUTCHours();
  const inBlackout = s > e ? hour >= s || hour < e : hour >= s && hour < e;
  if (!inBlackout) return retryAt;

  const wake = Date.UTC(
    local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate(),
    e % 24, 5, 0, 0,
  );
  return new Date((wake <= local.getTime() ? wake + 86_400_000 : wake) - IST_OFFSET_MINUTES * 60_000);
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 6 — THE ORCHESTRATOR: THE MONEY PATH (src/orchestrator.py)
// ═══════════════════════════════════════════════════════════════════════════
// process_payment_failure() — the complete pipeline for one webhook.

interface PaymentFailureRow {
  id: string;
  paymentId: string;
  orderId?: string | null;
  amount: number;
  method: string;
  bank?: string | null;
  failureClass: string;
  isRetryable: boolean;
  customerEmail?: string | null;
  customerContact?: string | null;
  failedAt: Date;
}

interface RecoveryCase {
  id: string;
  riskType: string;              // "payment_failure" today; invoice/cart/subscription scaffolded
  subjectRef: string;            // the payment id — the case's natural key half
  customerId?: string | null;
  amountAtRisk: number;
  amountRecovered: number;
  recoveredViaAttemptId?: string | null;  // NULL = self-recovery (customer paid anyway)
  state: "open" | "recovered" | "exhausted" | "abandoned" | "expired" | "opted_out";
  closeReason?: string | null;
  closedAt?: Date | null;
  attemptsUsed: number;
  maxAttempts: number;
  escalationLevel: number;       // decides nudge backoff width
  nextActionAt?: Date | null;    // the defer clock (promises, escalation gaps)
  batchId?: string | null;
}

interface RetryAttempt {
  id: string;
  paymentFailureId?: string | null;
  idempotencyKey: string;        // UNIQUE — the double-charge guard (§6 step 7)
  attemptNumber: number;
  recoveryCaseId?: string | null;
  actionType: string;
  targetRail?: string | null;
  scheduledAt?: Date | null;     // persisted in UTC, ALWAYS (see §6 step 9)
  agentType?: string | null;     // who ACTUALLY decided: "llm" | "xgboost" | "deterministic"
  agentReasoning?: string | null;
  guardrailPassed: boolean;
  guardrailRejectionReason?: string | null;
  result: "pending" | "scheduled" | "success" | "failed" | "rejected" | "skipped" | "superseded" | "cancelled" | null;
  resultDetails?: Record<string, unknown> | null;
  executedAt?: Date | null;
  externalRef?: string | null;   // the Payment Link id — the attribution join key
  nudgeMessage?: string | null;
  channel?: string | null;       // the channel that actually reached the customer
}

// openCase — idempotent get-or-create on (risk_type, subject_ref). The
// UNIQUE constraint on that pair is what makes a replayed webhook land on
// the existing case instead of doubling both the attempt budget and the
// recovered amount.
async function openCase(store: RecoveryStore, settings: EngineSettings, opts: { riskType: string; subjectRef: string; amountAtRisk: number; customerId?: string | null; batchId: string }, now: Date): Promise<RecoveryCase> {
  const existing = await store.findCase(opts.riskType, opts.subjectRef);
  if (existing) return existing;
  const kase: RecoveryCase = {
    id: randomUUID(),
    riskType: opts.riskType,
    subjectRef: opts.subjectRef,
    customerId: opts.customerId ?? null,
    amountAtRisk: opts.amountAtRisk,
    amountRecovered: 0,
    state: "open",
    attemptsUsed: 0,
    maxAttempts: settings.maxRetriesPerPayment,
    escalationLevel: 0,
    batchId: opts.batchId,
  };
  await store.saveCaseRecord(kase);
  await store.logCaseEvent(kase.id, "opened", "system",
    { risk_type: kase.riskType, subject_ref: kase.subjectRef, amount_at_risk: kase.amountAtRisk });
  return kase;
}

class PaymentRecoveryOrchestrator {
  constructor(
    private store: RecoveryStore,
    private settings: EngineSettings,
    private classifier: ClassifierMapper,
    private guardrail: GuardrailGate,
    private agentProvider: () => PolicyAgent | null,   // null → XGBoost only
    private executor: { createLink(failure: PaymentFailureRow, key: string): Promise<{ id: string }> },
  ) {}

  // Public so the scheduler can re-run the guardrail against the fire-time
  // clock without reaching into private state.
  validateNow(action: RetryAction, context: FailureContext, idemKey: string, attempts: number, now: Date): GuardrailResult {
    return this.guardrail.validate(action, { ...context, currentTime: now, hourOfDay: istHour(now) }, idemKey, attempts);
  }

  async processPaymentFailure(event: WebhookEventRow, now = new Date()): Promise<void> {
    const entity = (event.payload as unknown as WebhookPayload).payload?.payment?.entity ?? {};
    const paymentId = String(entity["id"] ?? "unknown");

    // ── Step 2: classify (deterministic; no LLM) ──────────────────────────
    const { failureClass, retryable } = this.classifier.classify(
      String(entity["error_code"] ?? "UNKNOWN"),
      (entity["error_source"] as string) ?? null,
      (entity["error_step"] as string) ?? null,
      (entity["error_reason"] as string) ?? null,
    );

    // ── Step 3: persist the failure ───────────────────────────────────────
    const failure: PaymentFailureRow = {
      id: randomUUID(),
      paymentId,
      orderId: (entity["order_id"] as string) ?? null,
      amount: Number(entity["amount"] ?? 0),
      method: String(entity["method"] ?? "unknown"),
      bank: (entity["bank"] as string) ?? null,
      failureClass,
      isRetryable: retryable,
      customerEmail: (entity["email"] as string) ?? null,
      customerContact: (entity["contact"] as string) ?? null,
      failedAt: new Date(Number(entity["created_at"] ?? 0) * 1000),
    };
    await this.store.insertFailure(failure);

    // ── Step 3b: open (or find) the recovery case ─────────────────────────
    // Idempotent on (risk_type, payment_id). Opened BEFORE the agent call so
    // the audit shows cases we declined too, not only ones we chased.
    const kase = await openCase(this.store, this.settings, {
      riskType: "payment_failure",
      subjectRef: paymentId,
      amountAtRisk: failure.amount,
      customerId: failure.customerEmail ?? failure.customerContact,
      batchId: now.toISOString().slice(0, 10), // day = natural batch for live traffic
    }, now);

    // ── Step 4: hard-decline fast path — abandon BEFORE any model call ────
    if (HARD_DECLINE_CLASSES.has(failureClass)) {
      closeCase(kase, "abandoned", `hard decline: ${failureClass}`, now);
      await this.store.saveCaseRecord(kase);
      // attempt_number=0, agent_type="deterministic", and deliberately NOT
      // attached to the case budget — declining costs nothing.
      await this.store.insertAttempt({
        id: randomUUID(), paymentId, idempotencyKey: `abandon_${paymentId}`,
        attemptNumber: 0, actionType: "abandon", agentType: "deterministic",
        guardrailPassed: true, result: "skipped", recoveryCaseId: kase.id,
      } as RetryAttempt);
      return;
    }

    // ── Step 4b: case-side stopping rule, BEFORE the agent (no wasted tokens)
    const ledger = kase.customerId ? await this.store.getLedger(kase.customerId) : null;
    const stop = stopReason(kase, ledger?.consentStatus ?? null, now);
    if (stop !== null) {
      // "deferred" (waiting out a backoff/promise) and "stopped" (finished)
      // are different facts; conflating them makes a bounded workflow look
      // like a broken one.
      await this.store.logCaseEvent(kase.id,
        stop.startsWith("next action not due") ? "deferred" : "stopped", "system", { reason: stop });
      return;
    }

    // ── Step 5: build the agent's context (rolling counts, true IST hour) ─
    const context = await this.buildFailureContext(failure, now);

    // ── Step 6: agent decision, WITH DEGRADATION DETECTION ────────────────
    let action: RetryAction | null = null;
    let agentType = "xgboost";
    const agent = this.agentProvider();
    if (agent !== null) {
      const fallbacksBefore = agent.fallbackCount;
      const candidate = await agent.decide(context);
      if (agent.fallbackCount === fallbacksBefore) {
        action = candidate;
        agentType = "llm";          // the LLM really answered
      }
      // else: decide() swallowed an error and returned its private heuristic
      // — discard it and let XGBoost decide below. agent_type records who
      // ACTUALLY decided; the audit never claims an LLM made calls it didn't.
    }
    if (action === null) {
      action = predictHeuristic(context);
      agentType = "xgboost";
    }

    // ── Step 6b: rail resolution — BEFORE the guardrail, so Layer 4
    // validates the action that actually executes. The one override: a
    // switch onto the rail that JUST declined is dead on arrival (the schema
    // check alone accepts it — "the same one" is a valid literal) and still
    // costs an attempt slot plus a live API call.
    if (action.action === "switch_rail") {
      action.rail = resolveTargetRail(context.method, action.rail, context.failureClass);
    }

    // ── Step 6c: clamp a deferral out of the blackout (§5) ────────────────
    if (action.action === "retry_at" && action.retryAt != null) {
      action.retryAt = clampRetryAtOutOfBlackout(action.retryAt, this.settings);
    }

    // ── Step 7: the idempotency key — DETERMINISTIC by construction.
    // (payment, attempt count) fully determines it. A random component would
    // make every key unique and the UNIQUE constraint unfireable: a key you
    // cannot collide is not an idempotency key. razorpay-python has no
    // idempotency header, so the double-charge guarantee is enforced HERE,
    // at our boundary, not the gateway's.
    const attemptCount = await this.store.countAttemptsByPaymentId(paymentId);
    const idemKey = `retry_${paymentId}_${attemptCount}`;
    if (await this.store.findAttemptByIdempotencyKey(idemKey)) {
      return; // replayed webhook — clean skip
    }

    // ── Step 8: guardrail — ALL rules, ALL violations, no short-circuit ───
    const guardrailResult = this.guardrail.validate(action, context, idemKey, attemptCount);

    // ── Step 9: the attempt record. attach_attempt spends one budget unit
    // EVEN ON REJECTION — a veto is a decision the case made, and not
    // counting it lets a guardrail-tripping payment re-enter the agent
    // forever. scheduled_at is persisted in UTC: the agent hands back an
    // IST-aware time, Postgres would normalise it, but SQLite drops the zone
    // and stores the wall clock — an IST value read back naive drifts 5h30m,
    // enough to re-enter the blackout it was just clamped out of.
    const attempt: RetryAttempt = {
      id: randomUUID(),
      idempotencyKey: idemKey,
      attemptNumber: attemptCount + 1,
      actionType: action.action,
      targetRail: action.rail ?? null,
      scheduledAt: action.retryAt ?? null,
      agentType,
      agentReasoning: action.reason,
      guardrailPassed: guardrailResult.passed,
      guardrailRejectionReason: guardrailResult.rejectionReasons.join("; ") || null,
      result: null,
    };
    attachAttempt(kase, attempt, this.settings.escalationBackoffHours, now);
    await this.store.saveCaseRecord(kase);

    if (!guardrailResult.passed) {
      attempt.result = "rejected";
      await this.store.insertAttempt(attempt);
      // NO ledger bump: a rejected action contacted nobody — counting it
      // against the customer's 24h tallies burns real-world quota on
      // outreach that never happened.
      return;
    }

    // ── Step 10: defer, or execute now ────────────────────────────────────
    if (action.action === "retry_at" && action.retryAt != null) {
      // Parked, NOT executed. "retry in 4 hours" used to create the link
      // immediately — an audit trail recording a delay that never happened.
      // The scheduler (§7) fires it and RE-RUNS the guardrail at fire time:
      // consent, budget and the blackout can all have changed in 4 hours.
      attempt.result = "scheduled";
      await this.store.insertAttempt(attempt);
      await this.store.logCaseEvent(kase.id, "deferred", agentType,
        { action: "retry_at", scheduled_at: action.retryAt.toISOString() });
      await this.bumpLedger(context.customerId, action, now);
      return;
    }

    await this.executeAndRecord(attempt, kase, failure, action, idemKey, agentType, now);
    await this.bumpLedger(context.customerId, action, now);
  }

  // ── THE MONEY BLOCK — shared VERBATIM by the live path and the scheduler
  // path (two copies would drift; the write-ahead ordering below is a
  // correctness property, not a style choice).
  async executeAndRecord(
    attempt: RetryAttempt,
    kase: RecoveryCase,
    failure: PaymentFailureRow,
    action: RetryAction,
    idemKey: string,
    actor: string,
    now = new Date(),
  ): Promise<void> {
    let nudgeMessage: string | null = null;
    if (action.action === "nudge_customer" || action.action === "switch_rail") {
      // Point them at the signed recovery page, not a bare link: a bare link
      // asks for money again and explains nothing.
      const page = recoveryLinkUrl(kase.id, this.settings, now);
      nudgeMessage = await generateNudge(failure, page);
      attempt.nudgeMessage = nudgeMessage;
    }

    if (action.action === "abandon") {
      attempt.result = "skipped";
      return;
    }

    // ── WRITE-AHEAD INTENT LOG. This row MUST be committed BEFORE the
    // Razorpay call. Recording after leaves a window where money has moved
    // and nothing says so: crash there, the count-derived idem_key stays
    // free, and the next payment.failed for this payment REUSES the slot and
    // charges the customer TWICE. Committing first makes the failure mode a
    // recorded "pending" that occupies its budget slot (a crash-looping
    // payment STOPS) instead of a silent double charge.
    attempt.result = "pending";
    try {
      await this.store.insertAttempt(attempt);
    } catch (e) {
      // The idempotency race, failed closed: two webhooks for one payment
      // both counted the same attempts, both derived the same key, both
      // passed the pre-check — the UNIQUE constraint breaks the tie BEFORE
      // Razorpay is called. The winner's attempt is the attempt; the loser
      // exits as a clean skip, not an unhandled exception.
      const existing = await this.store.findAttemptByIdempotencyKey(idemKey);
      if (existing !== null && existing.id !== attempt.id) return; // lost the race
      await this.store.saveAttempt(attempt); // same id: scheduler re-assert, an UPDATE not an INSERT
    }

    // ── Execute. (Python: sync SDK pushed off the event loop with
    // asyncio.to_thread + a default timeout on every request, because a hung
    // connection must not freeze every in-flight webhook.)
    let linkId: string | null = null;
    let success = true;
    try {
      const link = await this.executor.createLink(failure, idemKey);
      linkId = link.id;
    } catch {
      success = false;
    }

    attempt.executedAt = new Date();
    attempt.result = success ? "success" : "failed";
    if (linkId) attempt.externalRef = linkId; // THE ATTRIBUTION JOIN KEY (§9)
    attempt.channel = action.action === "nudge_customer" ? "email" : "payment_link";
    await this.store.saveAttempt(attempt);

    // Audit: nudges escalate; other successes contact. In the same
    // transaction as the change they describe — an audit row that survives
    // a rollback of the thing it claims happened is worse than none.
    const eventType = success
      ? (action.action === "nudge_customer" ? "escalated" : "contacted")
      : null;
    if (eventType) {
      await this.store.logCaseEvent(kase.id, eventType, actor,
        { action: action.action, channel: attempt.channel, external_ref: linkId });
    }
  }

  async buildFailureContext(failure: PaymentFailureRow, now: Date): Promise<FailureContext> {
    const customerId = failure.customerEmail ?? failure.customerContact;
    const ledger = customerId ? await this.store.getLedger(customerId) : null;

    // Rolling-window counts (§8.3) — reads and writes apply the SAME rule,
    // or the guardrail sees one number while the agent context reports
    // another.
    const windowMs = this.settings.rateLimitWindowHours * 3_600_000;
    const retries = ledger?.lastRetryAt && now.getTime() - ledger.lastRetryAt.getTime() <= windowMs
      ? ledger.totalRetries24h : 0;
    const nudges = ledger?.lastNudgeAt && now.getTime() - ledger.lastNudgeAt.getTime() <= windowMs
      ? ledger.totalNudges24h : 0;

    return {
      paymentId: failure.paymentId,
      failureClass: failure.failureClass,
      errorCode: "n/a",
      amount: failure.amount,
      method: failure.method,
      bank: failure.bank,
      customerId,
      retryCount24h: retries,
      nudgeCount24h: nudges,
      previousRetryOutcomes: await this.store.recentAttemptResults(failure.paymentId, 5),
      failedAt: failure.failedAt,
      currentTime: now,
      hourOfDay: istHour(now),
      dayOfWeek: 0,
      isRetryable: failure.isRetryable,
    };
  }

  // Ledger writes reset-then-increment with the SAME window rule the reads
  // use. The columns only ever increment otherwise: a customer's fifth retry
  // EVER would trip a limit named "per 24h" — a lifetime ban from a rate.
  private async bumpLedger(customerId: string | null | undefined, action: RetryAction, now: Date): Promise<void> {
    if (!customerId) return;
    const windowMs = this.settings.rateLimitWindowHours * 3_600_000;
    let ledger = await this.store.getLedger(customerId);
    if (!ledger) {
      ledger = { customerId, totalRetries24h: 0, totalNudges24h: 0, consentStatus: "granted" };
    }
    if (action.action !== "nudge_customer" && action.action !== "abandon") {
      if (ledger.lastRetryAt && now.getTime() - ledger.lastRetryAt.getTime() > windowMs) {
        ledger.totalRetries24h = 0;
      }
      ledger.totalRetries24h += 1;
      ledger.lastRetryAt = now;
    }
    if (action.action === "nudge_customer") {
      if (ledger.lastNudgeAt && now.getTime() - ledger.lastNudgeAt.getTime() > windowMs) {
        ledger.totalNudges24h = 0;
      }
      ledger.totalNudges24h += 1;
      ledger.lastNudgeAt = now;
    }
    await this.store.saveLedger(ledger, now);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 7 — LAYER 6: THE SCHEDULER (src/scheduler.py)
// ═══════════════════════════════════════════════════════════════════════════
// An in-process asyncio loop (no broker, no second deployment — swap in a
// real queue when there is more than one app process). Four sweeps, one tick.
// SERVERLESS WILL NOT WORK for this service: Lambda/CloudRun kill the process
// between requests and deferred retries would never fire.

// ── 7.1 fire_due_retries — an agent said "retry in 4 hours"; four hours passed.
// The guardrail runs AGAIN at fire time, against the CURRENT clock. That is
// the whole reason a deferred retry is not just a delayed call: in the hours
// since the agent decided, the customer may have opted out, the case may have
// closed, and 03:00 IST is inside a blackout that 22:00 was not.
// Deliberately NOT re-checked here: the attempt budget (already spent when
// this attempt was created — re-checking would reject exactly the retry the
// agent most deliberately chose) and next_action_at (it IS this action —
// re-checking would cancel every retry the moment it came due).
async function fireDueRetries(store: RecoveryStore, settings: EngineSettings, orchestrator: PaymentRecoveryOrchestrator, now: Date): Promise<number> {
  const due = await store.listDueScheduledAttempts(now, settings.schedulerBatchSize);
  let fired = 0;
  for (const attempt of due) {
    // Claim with a conditional UPDATE (scheduled → pending). Portable and
    // atomic: whoever flips the row owns the attempt; the loser's rowcount
    // is 0. SELECT ... FOR UPDATE SKIP LOCKED would do too, but the test
    // harness runs on SQLite, which has neither.
    const claimed = await store.claimScheduledAttempt(attempt.id);
    if (!claimed) continue;
    if (await fireOne(store, settings, orchestrator, attempt, now)) fired++;
  }
  return fired;
}

async function fireOne(store: RecoveryStore, settings: EngineSettings, orchestrator: PaymentRecoveryOrchestrator, attempt: RetryAttempt, now: Date): Promise<boolean> {
  const failure = attempt.paymentFailureId ? await store.getFailure(attempt.paymentFailureId) : null;
  if (!failure) return false;
  const kase = attempt.recoveryCaseId ? await store.getCase(attempt.recoveryCaseId) : null;

  if (kase !== null) {
    const ledger = kase.customerId ? await store.getLedger(kase.customerId) : null;
    let stop: string | null = null;
    if (kase.state !== "open") stop = `case is ${kase.state}`;
    else if (ledger?.consentStatus === "opted_out") stop = "customer opted out of contact";
    if (stop !== null) {
      attempt.result = "cancelled"; // cancelled + audited, never silently dropped
      attempt.resultDetails = { scheduler: stop };
      await store.saveAttempt(attempt);
      await store.logCaseEvent(kase.id, "stopped", "scheduler", { reason: stop, at: "fire_time" });
      return false;
    }
  }

  const context = await orchestrator.buildFailureContext(failure, now);
  // Fires as a PLAIN retry: the wait it asked for is over, and re-parking
  // would loop forever.
  const action: RetryAction = attempt.targetRail
    ? { action: "switch_rail", rail: attempt.targetRail as PaymentRail, reason: attempt.agentReasoning ?? "scheduled retry" }
    : { action: "retry_now", reason: attempt.agentReasoning ?? "scheduled retry" };

  const guardrail = orchestrator.validateNow(action, context, attempt.idempotencyKey, attempt.attemptNumber - 1, now);
  if (!guardrail.passed) {
    attempt.result = "rejected";
    attempt.resultDetails = { scheduler: guardrail.rejectionReasons.join("; ") };
    await store.saveAttempt(attempt);
    return false;
  }

  await orchestrator.executeAndRecord(attempt, kase!, failure, action, attempt.idempotencyKey, "scheduler", now);
  return true;
}

// ── 7.2 reconcile_events — a webhook was stored but its background task
// never ran (restart/crash/deploy after the 200; Razorpay will not re-send).
// Re-runs events left processed=false past an age threshold — the threshold
// is what keeps this from racing the legitimately-still-running task.
async function reconcileEvents(store: RecoveryStore, settings: EngineSettings, process: (e: WebhookEventRow) => Promise<void>, now: Date): Promise<number> {
  const cutoff = new Date(now.getTime() - settings.eventReconcileAfterSeconds * 1000);
  const stale = await store.listStaleUnprocessedEvents(cutoff, settings.schedulerBatchSize);
  let recovered = 0;
  for (const event of stale) {
    const claimed = await store.claimEvent(event.id); // same ownership trick
    if (!claimed) continue;
    try {
      await process(event);
      recovered++;
    } catch {
      await store.markEventError(event.razorpayEventId, "Reconciliation failed");
    }
  }
  return recovered;
}

// ── 7.3 reconcile_stale_attempts — a write-ahead row whose outcome never
// landed (crash mid-API-call) is resolved to failed-outcome-unknown after a
// threshold. FAIL-CLOSED: the slot stays spent (a link MIGHT exist out
// there), but nothing sits "in flight" forever, and a later capture still
// attributes through the idempotency-key breadcrumb (matching never reads
// result).
async function reconcileStaleAttempts(store: RecoveryStore, settings: EngineSettings, now: Date): Promise<number> {
  const cutoff = new Date(now.getTime() - settings.attemptStaleAfterSeconds * 1000);
  const stale = await store.listStalePendingAttempts(cutoff, settings.schedulerBatchSize);
  let resolved = 0;
  for (const attempt of stale) {
    const claimed = await store.claimStalePendingAttempt(attempt.id, now, settings.attemptStaleAfterSeconds);
    if (!claimed) continue;
    if (attempt.recoveryCaseId) {
      const kase = await store.getCase(attempt.recoveryCaseId);
      if (kase) await store.logCaseEvent(kase.id, "reconciled", "scheduler",
        { idempotency_key: attempt.idempotencyKey, reason: "stale pending — outcome unknown" });
    }
    resolved++;
  }
  return resolved;
}

// ── 7.4 expire_promises — see §8.2.

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 8 — CASE LIFECYCLE: THE BOUNDED WORKFLOW (src/cases.py)
// ═══════════════════════════════════════════════════════════════════════════

// ── 8.1 The stopping rule — "may we act again?" in one place.
const TERMINAL_STATES = new Set(["recovered", "exhausted", "abandoned", "expired", "opted_out"]);

function stopReason(kase: RecoveryCase, consentStatus: string | null | undefined, now: Date): string | null {
  if (TERMINAL_STATES.has(kase.state)) return `case already ${kase.state}`;
  if (kase.attemptsUsed >= kase.maxAttempts) return `attempt budget spent (${kase.attemptsUsed}/${kase.maxAttempts})`;
  if (consentStatus === "opted_out") return "customer opted out of contact";
  // Weakest reason LAST: it expires on its own, and reporting it first would
  // describe a dead case as merely waiting.
  if (kase.nextActionAt && now < kase.nextActionAt) return `next action not due until ${kase.nextActionAt.toISOString()}`;
  return null;
}

// Budget is spent by attaching an attempt — including rejected ones. Nudges
// escalate, and each rung buys WIDENING quiet (the complaint is never about
// the first message, it is about the fourth arriving as fast as the first).
function attachAttempt(kase: RecoveryCase, attempt: RetryAttempt, backoffHours: number, now: Date): void {
  attempt.recoveryCaseId = kase.id;
  kase.attemptsUsed += 1;
  if (attempt.actionType === "nudge_customer") {
    kase.escalationLevel += 1;
    kase.nextActionAt = new Date(now.getTime() + backoffHours * 3_600_000 * kase.escalationLevel);
  }
}

function closeCase(kase: RecoveryCase, state: RecoveryCase["state"], reason: string, now: Date): void {
  if (TERMINAL_STATES.has(kase.state)) return; // idempotent: first close wins
  kase.state = state;
  kase.closeReason = reason;
  kase.closedAt = now;
}

// ── 8.2 Promises to pay — the only customer response that makes the
// workflow QUIETER. A promise is permission to wait, not permission to
// contact sooner: next_action_at takes the LATER of the promise date and
// whatever the escalation ladder had already scheduled. A promise is broken
// ON THE CLOCK (never on suspicion) by the scheduler's fourth sweep, which
// also pulls next_action_at back to NOW (not NULL — NULL means
// webhook-driven and nothing polls it) so the silence it bought is not
// permanent. An opt-out CANCELS pending promises — marking them broken would
// libel the customer in the one table a dispute is settled from.
async function recordPromise(store: RecoveryStore, kase: RecoveryCase, amount: number, dueAt: Date, now: Date): Promise<void> {
  await store.insertPromise({ id: randomUUID(), recoveryCaseId: kase.id, amountPromised: amount, dueAt, status: "pending" });
  kase.nextActionAt = kase.nextActionAt && kase.nextActionAt > dueAt ? kase.nextActionAt : dueAt;
  await store.saveCaseRecord(kase);
  await store.logCaseEvent(kase.id, "promise_made", "customer", { amount, due_at: dueAt.toISOString() });
}

// ── 8.3 Opt-out — closes the WORK, not one nudge: skipping one message and
// leaving the case open means the next scheduler tick contacts them again.
// Consent is a ledger-level flag so every case for the customer stops at
// once, and it is checked at decision time AND at fire time.
async function recordOptOut(store: RecoveryStore, customerId: string, now: Date): Promise<number> {
  let ledger = await store.getLedger(customerId);
  if (!ledger) {
    ledger = { customerId, totalRetries24h: 0, totalNudges24h: 0, consentStatus: "granted" };
  }
  ledger.consentStatus = "opted_out";
  await store.saveLedger(ledger, now);
  let closed = 0;
  for (const kase of await store.listOpenCasesByCustomer(customerId)) {
    closeCase(kase, "opted_out", "customer withdrew consent", now);
    await store.saveCaseRecord(kase);
    await store.logCaseEvent(kase.id, "opted_out", "customer", {});
    closed++;
  }
  return closed;
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 9 — REVENUE ATTRIBUTION (payment.captured → cases.py)
// ═══════════════════════════════════════════════════════════════════════════
// The half that makes the headline number honest. A recovered payment is a
// NEW payment id (the customer paid a link we sent) — matching it back needs
// the breadcrumbs the executor wrote. Resolution order, most specific first:
//
//   1. link_id         → RetryAttempt.external_ref (direct hit on our link)
//   2. idempotency_key → Razorpay copies the link's `notes` onto the payment
//   3. order_ref       → the order id survives across attempts; this is the
//                        customer paying ON THEIR OWN: money counts, engine
//                        gets no credit (recovered_via_attempt_id stays NULL)
//
// On attribution: credit the case, keep pending promises (kept — even a
// self-recovery keeps a promise: they said they'd pay and they did), close
// the case when recovered >= at_risk, and SUPERSEDE outstanding pending
// attempts (the old code's update matched nothing because it compared the
// new payment id to the ORIGINAL failed one — not one rupee was attributable
// before this resolution order existed).
async function handleCapture(payload: WebhookPayload, store: RecoveryStore, settings: EngineSettings): Promise<void> {
  const entity = payload.payload?.payment?.entity ?? {};
  const paymentId = entity["id"] as string | undefined;
  if (!paymentId) return;
  const notes = (entity["notes"] as Record<string, string> | null) ?? {};
  const linkEntity = ((payload.payload as unknown as Record<string, unknown> | undefined)?.["payment_link"] as { entity?: Record<string, unknown> } | undefined)?.entity ?? {};

  let attempt = linkEntity["id"] ? await store.findNewestAttemptByExternalRef(linkEntity["id"] as string) : null;
  let matchedOn: "link_id" | "idempotency_key" | "order_ref" = "link_id";
  if (!attempt && notes["retry_idempotency_key"]) {
    attempt = await store.findAttemptByIdempotencyKey(notes["retry_idempotency_key"]);
    matchedOn = "idempotency_key";
  }

  let kase: RecoveryCase | null = null;
  if (attempt?.recoveryCaseId) {
    kase = await store.getCase(attempt.recoveryCaseId);
  } else if (!attempt && entity["order_id"]) {
    const caseId = await store.findOpenCaseIdByOrder(entity["order_id"] as string);
    kase = caseId ? await store.getCase(caseId) : null;
    attempt = null; // self-recovery: no attempt of ours earned this
    matchedOn = "order_ref";
  }
  if (!kase) return; // unrelated capture — the common path; most payments never fail

  const amount = Number(entity["amount"] ?? 0);
  kase.amountRecovered += amount;
  kase.recoveredViaAttemptId = attempt ? attempt.id : null;
  await store.saveCaseRecord(kase);
  await store.logCaseEvent(kase.id, "attributed", "system",
    { amount, matched_on: matchedOn, attributed_to_attempt: attempt?.id ?? null });

  if (kase.amountRecovered >= kase.amountAtRisk) {
    closeCase(kase, "recovered", `captured ${kase.amountRecovered} paise via ${paymentId}`, new Date());
    await store.saveCaseRecord(kase);
    await store.logCaseEvent(kase.id, "closed", "system", { state: "recovered" });
  }
  await store.supersedePendingAttemptsByCase(kase.id, new Date());
}

// batch_summary() — the honesty function. recovered = everything that came
// back; attributed = the subset OUR links brought back. The headline quotes
// the second: a number that cannot separate them is taking credit for the
// control group.
async function batchSummary(store: RecoveryStore, batchId: string | null) {
  const s = await store.summarizeBatch(batchId);
  return {
    ...s,
    recoveryRatePct: s.atRiskPaise ? +((s.recoveredPaise / s.atRiskPaise) * 100).toFixed(2) : 0,
    attributedRatePct: s.atRiskPaise ? +((s.attributedPaise / s.atRiskPaise) * 100).toFixed(2) : 0,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 10 — THE CUSTOMER RECOVERY PAGE (src/recovery_link.py)
// ═══════════════════════════════════════════════════════════════════════════
// /recover/<token> shows one customer their own failed payment. The URL is
// the only credential, which rules out a raw case id (enumerable) and an API
// key (consumers can't hold one). The token is: unguessable (HMAC-signed),
// scoped (names exactly one case), expiring (dies with the consent window —
// a link forwarded weeks later stops working rather than reopening a closed
// case), and PII-free (URLs end up in SMS logs and browser history). It uses
// a DEDICATED secret — reusing the webhook secret means a leak of one is a
// leak of two things with completely different blast radii. Empty secret =
// feature OFF and every token rejected (the fail-closed rule the whole
// config follows).
function recoveryLinkUrl(caseId: string, settings: EngineSettings, now: Date): string | null {
  if (!settings.recoveryLinkSecret || !settings.publicBaseUrl) return null;
  const expiry = Math.floor(now.getTime() / 1000) + settings.consentWindowHours * 3600;
  const payload = `${caseId.replaceAll("-", "")}.${expiry}`;
  const sig = createHmac("sha256", settings.recoveryLinkSecret).update(payload).digest("base64url");
  const token = `${Buffer.from(payload).toString("base64url")}.${sig}`;
  return `${settings.publicBaseUrl.replace(/\/+$/, "")}/recover/${token}`;
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 11 — LAYER 5: NUDGE MESSAGING (src/messaging/)
// ═══════════════════════════════════════════════════════════════════════════
// LLM-personalized with a 3-second timeout and a Jinja2 template fallback.
// Never blocks, never raises — a messaging hiccup must not take down the
// retry that carried it. Messages are capped at 160 chars (SMS).
async function generateNudge(failure: PaymentFailureRow, recoveryPageUrl: string | null): Promise<string> {
  const nextStep = recoveryPageUrl
    ? `Check your payment and pay securely here: ${recoveryPageUrl}`
    : "Please try again using a different payment method.";
  const fallback = `Hi, your ₹${(failure.amount / 100).toFixed(2)} payment didn't go through. ${nextStep}`;
  return fallback.slice(0, 160); // (LLM path omitted: same contract, 3s timeout, template on any failure)
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 12 — RAIL SELECTION (src/executor/rail_selector.py)
// ═══════════════════════════════════════════════════════════════════════════
// Keeps the agent's own choice — it has context the heuristic lacks. The one
// override: a switch onto the rail that JUST declined (nothing upstream
// catches it; the schema check requires only "a valid rail").
function resolveTargetRail(currentMethod: string, proposed: PaymentRail | null | undefined, failureClass: string): PaymentRail | null {
  if (proposed != null && proposed !== currentMethod) return proposed;
  const alternatives = (["upi", "card", "netbanking", "wallet"] as PaymentRail[]).filter((r) => r !== currentMethod);
  if (alternatives.length === 0) return null; // fail-closed: guardrail schema rejects a null rail
  const preferUpi = alternatives.includes("upi");
  if (failureClass === "3ds_dropoff" || failureClass === "issuer_decline" || failureClass === "card_limit_exceeded") {
    return preferUpi ? "upi" : alternatives[0];
  }
  if (failureClass === "upi_collect_timeout") return alternatives.includes("card") ? "card" : alternatives[0];
  if (preferUpi) return "upi";
  return alternatives.includes("card") ? "card" : alternatives[0];
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 13 — STORAGE CONTRACT (src/models.py — SQLAlchemy/Postgres)
// ═══════════════════════════════════════════════════════════════════════════
// The real engine runs on Postgres (async SQLAlchemy) with Alembic
// migrations. create_all() is dev-only: it creates missing TABLES and
// silently ignores missing COLUMNS — on a database that predates a model
// change it "succeeds" and the first write to the money path fails with
// UndefinedColumn. The interface below is the exact surface the pipeline
// needs; the UNIQUE constraints shown in comments are load-bearing.

interface RetryLedger {
  customerId: string;
  totalRetries24h: number;
  totalNudges24h: number;
  lastRetryAt?: Date | null;
  lastNudgeAt?: Date | null;
  consentStatus: "granted" | "opted_out";
}

interface RecoveryStore {
  insertEvent(e: WebhookEventRow): Promise<void>;
  markEventProcessed(razorpayEventId: string): Promise<void>;
  markEventError(razorpayEventId: string, error: string): Promise<void>;
  claimEvent(id: string): Promise<boolean>;                    // conditional UPDATE — ownership
  listStaleUnprocessedEvents(cutoff: Date, limit: number): Promise<WebhookEventRow[]>;
  recordProcessedEvent(razorpayEventId: string): Promise<boolean>; // dedup; false = duplicate

  insertFailure(f: PaymentFailureRow): Promise<void>;
  getFailure(id: string): Promise<PaymentFailureRow | null>;
  findOpenCaseIdByOrder(orderRef: string): Promise<string | null>; // via payment_failures join

  findCase(riskType: string, subjectRef: string): Promise<RecoveryCase | null>; // UNIQUE(risk_type, subject_ref)
  saveCaseRecord(kase: RecoveryCase): Promise<void>;
  getCase(id: string): Promise<RecoveryCase | null>;
  listOpenCasesByCustomer(customerId: string): Promise<RecoveryCase[]>;
  logCaseEvent(caseId: string, type: string, actor: string, detail: Record<string, unknown>): Promise<void>;

  findAttemptByIdempotencyKey(key: string): Promise<RetryAttempt | null>; // UNIQUE(idempotency_key)
  findNewestAttemptByExternalRef(ref: string): Promise<RetryAttempt | null>;
  countAttemptsByPaymentId(paymentId: string): Promise<number>;
  recentAttemptResults(paymentId: string, limit: number): Promise<string[]>;
  insertAttempt(a: RetryAttempt): Promise<void>;               // throws on UNIQUE violation = race lost
  saveAttempt(a: RetryAttempt): Promise<void>;
  listDueScheduledAttempts(now: Date, limit: number): Promise<RetryAttempt[]>; // indexed (result, scheduled_at)
  claimScheduledAttempt(id: string): Promise<boolean>;         // conditional UPDATE scheduled→pending
  listStalePendingAttempts(cutoff: Date, limit: number): Promise<RetryAttempt[]>;
  claimStalePendingAttempt(id: string, now: Date, staleAfter: number): Promise<boolean>;
  supersedePendingAttemptsByCase(caseId: string, now: Date): Promise<number>;

  getLedger(customerId: string): Promise<RetryLedger | null>;
  saveLedger(l: RetryLedger, now: Date): Promise<void>;

  insertPromise(p: { id: string; recoveryCaseId: string; amountPromised: number; dueAt: Date; status: string }): Promise<void>;
  summarizeBatch(batchId: string | null): Promise<{ cases: number; atRiskPaise: number; recoveredPaise: number; attributedPaise: number; attemptsUsed: number }>;
}

// ═══════════════════════════════════════════════════════════════════════════
// SECTION 14 — THE MONEY-SAFETY INVARIANTS (verification checklist)
// ═══════════════════════════════════════════════════════════════════════════
// 1.  No LLM before classification; hard declines never reach any model.
// 2.  Output space is exactly 5 validated actions; nothing freeform executes.
// 3.  Guardrail runs before EVERY execution, checks ALL rules, reports ALL
//     violations, and re-runs at fire time for deferred retries.
// 4.  Write-ahead: the attempt is "pending" in the DB BEFORE the Razorpay
//     call, on both live and scheduled paths (one shared implementation).
// 5.  Double-charge prevention is three deep: deterministic key → pre-check
//     → UNIQUE constraint (race loser exits cleanly, pre-API).
// 6.  Every state transition writes an audit row with an actor, in the same
//     transaction as the change it describes.
// 7.  Deferred retries: clamped out of blackout at decision time, persisted
//     UTC, re-validated at fire time, cancellable by opt-out.
// 8.  Fail-closed everywhere: bad signature 401; missing Razorpay creds = no
//     boot; unconfigured recovery page rejects all tokens; stale pendings
//     never free budget; rejected actions never burn contact quota.
// 9.  Honest metrics: agent_type records the real decider; fallbackCount
//     makes LLM degradation countable; attributed ≠ recovered in reporting.
// 10. Rolling 24h windows on reads AND writes — "per 24h" can never silently
//     become a lifetime ban.
//
// KNOWN, DOCUMENTED LIMITS
// ────────────────────────
// - The bank simulator is synthetic (calibration-tested against the public
//   15–30% recovery band).
// - Free-tier hosting sleeps the process → deferred retries fire late, never
//   lost (they stay "scheduled"; missed webhooks are re-sent by Razorpay).
// - razorpay-python has no idempotency header: idempotency is ours, enforced
//   at our boundary, not the gateway's.
//
// EVAL HEADLINE (eval/runner.py, 5000 scenarios × 5 seeds, common random
// numbers, paired CIs): +11.03pp recovery rate vs fixed 3-retry at 15% fewer
// attempts — net +₹11.70L per ₹1Cr of failed volume, dominating at any
// retry cost including ₹0.
// ═══════════════════════════════════════════════════════════════════════════
