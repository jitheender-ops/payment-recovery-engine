import assert from "node:assert/strict";
import test from "node:test";
import { createHmac } from "node:crypto";
import { InMemoryStore } from "./store.js";
import { DEFAULT_SETTINGS, type EngineSettings } from "./settings.js";
import { PaymentRecoveryOrchestrator } from "./orchestrator.js";
import { RetryExecutor } from "./executor.js";
import { receiveRazorpayWebhook, processEventBackground } from "./ingestion.js";
import { tick, reconcileEvents } from "./scheduler.js";
import { attributeCapture, recordPromise, batchSummary, recordOptOut } from "./cases.js";
import * as recoveryLink from "./recoveryLink.js";
import { PolicyAgent, LlmError } from "./llmAgent.js";
import type { PolicyDecider } from "./orchestrator.js";
import type { RetryAction } from "./actions.js";
import type { RazorpayClient } from "./executor.js";
import { FailureClass } from "./taxonomy.js";

const SECRET = "test-webhook-secret";

function settings(overrides: Partial<EngineSettings> = {}): EngineSettings {
  return {
    ...DEFAULT_SETTINGS,
    razorpayWebhookSecret: SECRET,
    recoveryLinkSecret: "link-secret-do-not-share",
    publicBaseUrl: "https://recovery-api.onrender.com",
    ...overrides,
  };
}

function fakeRazorpay(): RazorpayClient & { links: Array<Record<string, unknown>>; notifications: string[] } {
  const links: Array<Record<string, unknown>> = [];
  const notifications: string[] = [];
  let n = 0;
  return {
    links,
    notifications,
    async createPaymentLink(data) {
      const id = `plink_${++n}`;
      const record = { id, short_url: `https://rzp.io/i/${id}`, ...data };
      links.push(record);
      return record;
    },
    async notifyBy(linkId, channel) {
      notifications.push(`${channel}:${linkId}`);
    },
  };
}

function failedPayload(opts: {
  paymentId?: string;
  amount?: number;
  method?: string;
  errorCode?: string;
  errorSource?: string;
  errorStep?: string;
  errorReason?: string;
  email?: string;
  createdAt?: number;
}): Record<string, unknown> {
  return {
    event: "payment.failed",
    created_at: opts.createdAt ?? 1787712000,
    payload: {
      payment: {
        entity: {
          id: opts.paymentId ?? "pay_TEST001",
          order_id: "order_TEST001",
          amount: opts.amount ?? 150000,
          currency: "INR",
          method: opts.method ?? "card",
          bank: "HDFC",
          status: "failed",
          error_code: opts.errorCode ?? "GATEWAY_ERROR",
          error_source: opts.errorSource ?? "gateway",
          error_step: opts.errorStep,
          error_reason: opts.errorReason,
          email: opts.email ?? "customer@example.com",
          card: { network: "Visa", type: "credit", issuer: "HDFC" },
          created_at: opts.createdAt ?? 1787712000,
        },
      },
    },
  };
}

function sign(body: string): string {
  return createHmac("sha256", SECRET).update(body, "utf8").digest("hex");
}

interface Harness {
  store: InMemoryStore;
  orch: PaymentRecoveryOrchestrator;
  rzp: ReturnType<typeof fakeRazorpay>;
  deps: {
    store: InMemoryStore;
    settings: EngineSettings;
    orchestrator: PaymentRecoveryOrchestrator;
  };
  agentCalls: { count: number };
}

function harness(overrides: Partial<EngineSettings> = {}, agent: PolicyDecider | null = null): Harness {
  const store = new InMemoryStore();
  const s = settings(overrides);
  const rzp = fakeRazorpay();
  const agentCalls = { count: 0 };
  const wrapped: PolicyDecider | null =
    agent === null
      ? null
      : {
          async decide(ctx) {
            agentCalls.count++;
            return agent.decide(ctx);
          },
          get fallbackCount() {
            return agent.fallbackCount;
          },
        };
  const orch = new PaymentRecoveryOrchestrator({
    store,
    settings: s,
    executor: new RetryExecutor(rzp),
    agent: wrapped,
    agentFactory: () => wrapped,
  });
  const deps = { store, settings: s, orchestrator: orch };
  return { store, orch, rzp, deps, agentCalls };
}

test("webhook: bad signature fails closed with 401", async () => {
  const h = harness();
  const body = JSON.stringify(failedPayload({}));
  const ack = await receiveRazorpayWebhook(body, "deadbeef", h.deps);
  assert.equal(ack.status, 401);
  assert.equal((await h.store.summarizeBatch(null)).cases, 0);
});

test("webhook: full happy path — classify, decide, guardrail, execute, ledger", async () => {
  const h = harness();
  const body = JSON.stringify(
    failedPayload({ errorCode: "BAD_REQUEST_ERROR", errorSource: "customer", errorReason: "insufficient_funds" }),
  );
  const ack = await receiveRazorpayWebhook(body, sign(body), h.deps);
  assert.equal(ack.status, 200);

  const summary = await h.store.summarizeBatch(null);
  assert.equal(summary.cases, 1);
  assert.equal(summary.attemptsUsed, 1);
  assert.equal(h.rzp.links.length, 1);

  const link = h.rzp.links[0];
  const notes = link["notes"] as Record<string, string>;
  assert.equal(notes["original_payment_id"], "pay_TEST001");
  assert.match(notes["retry_idempotency_key"], /^retry_pay_TEST001_\d$/);

  const ledger = await h.store.getLedger("customer@example.com");
  assert.equal(ledger?.totalNudges24h, 1);
  assert.equal(ledger?.totalRetries24h ?? 0, 0);
});

test("webhook: duplicate event id is skipped", async () => {
  const h = harness();
  const body = JSON.stringify(failedPayload({}));
  await receiveRazorpayWebhook(body, sign(body), h.deps);
  const second = await receiveRazorpayWebhook(body, sign(body), h.deps);
  assert.equal(second.status, 200);
  assert.equal(second.body, "Already processed");
  assert.equal((await h.store.summarizeBatch(null)).attemptsUsed, 1);
});

test("hard decline: abandoned deterministically, agent never called, no budget spent", async () => {
  const h = harness({
    recoveryLinkSecret: "",
    publicBaseUrl: "",
  }, {
    async decide() {
      throw new Error("agent must not be called for hard declines");
    },
    fallbackCount: 0,
  });
  const body = JSON.stringify(
    failedPayload({ errorCode: "BAD_REQUEST_ERROR", errorSource: "customer", errorReason: "payment_risk_check_failed" }),
  );
  await receiveRazorpayWebhook(body, sign(body), h.deps);

  const kase = h.store.findCaseByKey("payment_failure", "pay_TEST001");
  assert.equal(kase?.state, "abandoned");
  assert.equal(kase?.attemptsUsed, 0);
  assert.equal(h.agentCalls.count, 0);
  assert.equal(h.rzp.links.length, 0);
});

test("guardrail rejection: attempt recorded as rejected, no Razorpay call, no ledger bump", async () => {
  const h = harness();
  const body = JSON.stringify(
    failedPayload({ amount: 9_000_000, errorCode: "BAD_REQUEST_ERROR", errorSource: "customer", errorReason: "insufficient_funds" }),
  );
  await receiveRazorpayWebhook(body, sign(body), h.deps);

  assert.equal(h.rzp.links.length, 0);
  const ledger = await h.store.getLedger("customer@example.com");
  assert.equal(ledger?.totalRetries24h ?? 0, 0);

  const kase = h.store.findCaseByKey("payment_failure", "pay_TEST001");
  assert.equal(kase?.attemptsUsed, 1);
  const events = await h.store.listCaseEvents(kase!.id);
  assert.ok(events.every((e) => e.eventType !== "contacted"));
});

test("retry_at parks as scheduled; scheduler fires it later after re-validation", async () => {
  const now = new Date("2026-08-26T10:00:00Z");
  const h = harness({}, {
    async decide(): Promise<RetryAction> {
      return {
        action: "retry_at",
        retryAt: new Date(now.getTime() + 30 * 60_000),
        reason: "bank recovering, wait 30 minutes",
        confidence: 0.7,
      };
    },
    fallbackCount: 0,
  });

  const body = JSON.stringify(
    failedPayload({ errorCode: "GATEWAY_ERROR", errorSource: "gateway", errorReason: "issuer_down" }),
  );
  await receiveRazorpayWebhook(body, sign(body), h.deps, now);
  assert.equal(h.rzp.links.length, 0);

  const kase = h.store.findCaseByKey("payment_failure", "pay_TEST001");
  const events = await h.store.listCaseEvents(kase!.id);
  assert.ok(events.some((e) => e.eventType === "deferred"));

  const before = await tick(h.deps, new Date(now.getTime() + 60_000));
  assert.equal(before.retries_fired, 0);

  const after = await tick(h.deps, new Date(now.getTime() + 31 * 60_000));
  assert.equal(after.retries_fired, 1);
  assert.equal(h.rzp.links.length, 1);
});

test("opt-out during a deferred wait cancels the retry at fire time", async () => {
  const now = new Date("2026-08-26T10:00:00Z");
  const h = harness({}, {
    async decide(): Promise<RetryAction> {
      return {
        action: "retry_at",
        retryAt: new Date(now.getTime() + 30 * 60_000),
        reason: "wait for the customer",
        confidence: 0.6,
      };
    },
    fallbackCount: 0,
  });

  const body = JSON.stringify(
    failedPayload({ errorCode: "GATEWAY_ERROR", errorSource: "gateway", errorReason: "bank_unavailable" }),
  );
  await receiveRazorpayWebhook(body, sign(body), h.deps, now);

  await recordOptOut(h.store, "customer@example.com", new Date(now.getTime() + 60_000));

  const counts = await tick(h.deps, new Date(now.getTime() + 31 * 60_000));
  assert.equal(counts.retries_fired, 0);
  assert.equal(h.rzp.links.length, 0);

  const kase = h.store.findCaseByKey("payment_failure", "pay_TEST001");
  assert.equal(kase?.state, "opted_out");
});

test("capture attribution: via link_id, closes the case, supersedes pending", async () => {
  const h = harness();
  const body = JSON.stringify(
    failedPayload({ errorCode: "BAD_REQUEST_ERROR", errorSource: "customer", errorReason: "insufficient_funds" }),
  );
  await receiveRazorpayWebhook(body, sign(body), h.deps);

  const linkId = h.rzp.links[0]["id"] as string;
  const capture = JSON.stringify({
    event: "payment.captured",
    created_at: 1787713000,
    payload: {
      payment: {
        entity: {
          id: "pay_NEWID1",
          order_id: "order_TEST001",
          amount: 150000,
          notes: {},
        },
      },
      payment_link: { entity: { id: linkId } },
    },
  });

  await processEventBackground(
    `payment.captured_pay_NEWID1_1787713000`,
    "payment.captured",
    JSON.parse(capture),
    h.deps,
  );

  const kase = h.store.findCaseByKey("payment_failure", "pay_TEST001");
  assert.equal(kase?.state, "recovered");
  assert.equal(kase?.amountRecovered, 150000);
  assert.notEqual(kase?.recoveredViaAttemptId, null);

  const summary = await batchSummary(h.store, null);
  assert.equal(summary.attributedPaise, 150000);
  assert.equal(summary.recoveryRatePct, 100);
});

test("capture attribution: order-only capture is self-recovery — revenue but no credit", async () => {
  const h = harness();
  const body = JSON.stringify(
    failedPayload({ errorCode: "BAD_REQUEST_ERROR", errorSource: "customer", errorReason: "insufficient_funds" }),
  );
  await receiveRazorpayWebhook(body, sign(body), h.deps);

  const kaseBefore = h.store.findCaseByKey("payment_failure", "pay_TEST001");
  await attributeCapture(h.store, settings(), {
    amount: 150000,
    recoveredRef: "pay_SELFPAID",
    orderRef: "order_TEST001",
  });

  const kase = h.store.findCaseByKey("payment_failure", "pay_TEST001");
  assert.equal(kase?.state, "recovered");
  assert.equal(kase?.recoveredViaAttemptId, null);
  assert.equal(kase?.id, kaseBefore?.id);

  const summary = await batchSummary(h.store, null);
  assert.equal(summary.recoveredPaise, 150000);
  assert.equal(summary.attributedPaise, 0);
});

test("stale pending attempt resolves fail-closed via the scheduler", async () => {
  const now = new Date("2026-08-26T10:00:00Z");
  const h = harness({ attemptStaleAfterSeconds: 900 });

  const kase = await (await import("./cases.js")).openCase(h.store, h.deps.settings, {
    riskType: "payment_failure",
    subjectRef: "pay_STALE1",
    amountAtRisk: 100000,
    customerId: "stale@example.com",
  }, now);

  const orphan: import("./entities.js").RetryAttempt = {
    id: "a_stale_1",
    paymentFailureId: null,
    paymentId: "pay_STALE1",
    idempotencyKey: "retry_pay_STALE1_0",
    attemptNumber: 1,
    recoveryCaseId: kase.id,
    actionType: "retry_now",
    guardrailPassed: true,
    result: "pending",
    nudgeSent: false,
    createdAt: now,
  };
  await h.store.insertAttempt(orphan);

  const early = await tick(h.deps, new Date(now.getTime() + 100_000));
  assert.equal(early.attempts_reconciled, 0);

  const counts = await tick(h.deps, new Date(now.getTime() + 901_000));
  assert.equal(counts.attempts_reconciled, 1);

  const resolved = await h.store.getAttempt("a_stale_1");
  assert.equal(resolved?.result, "failed");
  assert.match(JSON.stringify(resolved?.resultDetails), /fail-closed/);

  const events = await h.store.listCaseEvents(kase.id);
  assert.ok(events.some((e) => e.eventType === "reconciled"));
});

test("promise: record buys quiet, expiry hands the case back", async () => {
  const now = new Date("2026-08-26T10:00:00Z");
  const h = harness();
  const body = JSON.stringify(
    failedPayload({ errorCode: "BAD_REQUEST_ERROR", errorSource: "customer", errorReason: "insufficient_funds" }),
  );
  await receiveRazorpayWebhook(body, sign(body), h.deps, now);

  const kase = h.store.findCaseByKey("payment_failure", "pay_TEST001");
  await recordPromise(h.store, kase!, {
    amount: 150000,
    dueAt: new Date(now.getTime() + 24 * 3_600_000),
    channel: "whatsapp",
  });

  const stopped = stopCheck(h, now);
  assert.match(stopped ?? "", /next action not due/);

  const counts = await tick(h.deps, new Date(now.getTime() + 25 * 3_600_000));
  assert.equal(counts.promises_expired, 1);

  const events = await h.store.listCaseEvents(kase!.id);
  assert.ok(events.some((e) => e.eventType === "promise_broken"));
  assert.ok(kase!.nextActionAt !== null);
});

function stopCheck(h: Harness, now: Date): string | null {
  const kase = h.store.findCaseByKey("payment_failure", "pay_TEST001");
  if (!kase) return "no case";
  if (kase.nextActionAt && kase.nextActionAt > now) {
    return `next action not due until ${kase.nextActionAt.toISOString()}`;
  }
  return null;
}

test("reconcileEvents re-runs a webhook whose processing never happened", async () => {
  const now = new Date("2026-08-26T10:00:00Z");
  const h = harness({ eventReconcileAfterSeconds: 300 });

  const payload = failedPayload({
    errorCode: "BAD_REQUEST_ERROR",
    errorSource: "customer",
    errorReason: "insufficient_funds",
  });
  const eventId = `payment.failed_pay_TEST001_1787712000`;
  await h.store.recordProcessedEvent(eventId);
  await h.store.insertEvent({
    id: "evt_1",
    razorpayEventId: eventId,
    eventType: "payment.failed",
    payload,
    receivedAt: new Date(now.getTime() - 600_000),
    processed: false,
    processingError: null,
  });

  const recovered = await reconcileEvents(h.deps, now, async (event) => {
    if (event) await h.orch.processPaymentFailure(event);
  });
  assert.equal(recovered, 1);
  assert.equal(h.rzp.links.length, 1);
});

test("recovery link: mint, verify, expiry, tamper, fail-closed without secret", () => {
  const caseId = "0f0e0d0c-0b0a-0908-0706-050403020100";
  const now = new Date("2026-08-26T10:00:00Z");

  const token = recoveryLink.mint(caseId, "s3cret", 72, now);
  assert.ok(token);
  assert.equal(recoveryLink.verify(token!, "s3cret", now), caseId);

  const expired = recoveryLink.verify(token!, "s3cret", new Date(now.getTime() + 73 * 3_600_000));
  assert.equal(expired, null);

  const tampered = token!.slice(0, -2) + (token!.endsWith("aa") ? "bb" : "aa");
  assert.equal(recoveryLink.verify(tampered, "s3cret", now), null);
  assert.equal(recoveryLink.verify(token!, "wrong-secret", now), null);

  assert.equal(recoveryLink.mint(caseId, "", 72, now), null);
  assert.equal(recoveryLink.verify(token!, "", now), null);

  const url = recoveryLink.urlFor(caseId, "s3cret", "https://api.example.com/", 72, now);
  assert.equal(url, `https://api.example.com/recover/${token}`);
});

test("LLM degradation: a fallback-counting agent hands the decision to XGBoost", async () => {
  const now = new Date("2026-08-26T10:00:00Z");
  let calls = 0;
  const degraded: PolicyDecider = {
    async decide(): Promise<RetryAction> {
      calls++;
      return {
        action: "abandon",
        reason: "Fallback: conservative abandon (LLM error: HTTP 402)",
        confidence: 0.3,
      };
    },
    get fallbackCount() {
      return calls;
    },
  };

  const h = harness({}, degraded);
  const body = JSON.stringify(
    failedPayload({ errorCode: "BAD_REQUEST_ERROR", errorSource: "customer", errorReason: "insufficient_funds" }),
  );
  await receiveRazorpayWebhook(body, sign(body), h.deps, now);

  assert.equal(h.agentCalls.count, 1);

  const kase = h.store.findCaseByKey("payment_failure", "pay_TEST001");
  assert.notEqual(kase, null);
  assert.equal(kase?.state, "open");

  const events = await h.store.listCaseEvents(kase!.id);
  assert.ok(events.some((e) => e.eventType === "escalated"));
});

test("PolicyAgent: transient error retried once, then success", async () => {
  let calls = 0;
  const client = {
    async complete(): Promise<string> {
      calls++;
      if (calls === 1) throw new LlmError("429 too many requests", 429);
      return JSON.stringify({
        action: "retry_now",
        reason: "transient gateway error, immediate retry",
        confidence: 0.8,
      });
    },
  };
  const agent = new PolicyAgent(settings(), client, async () => {});
  const action = await agent.decide({
    paymentId: "pay_T",
    failureClass: FailureClass.NetworkError,
    errorCode: "GATEWAY_ERROR",
    amount: 10000,
    currency: "INR",
    method: "upi",
    retryCount24h: 0,
    nudgeCount24h: 0,
    previousRetryOutcomes: [],
    failedAt: new Date("2026-08-26T09:00:00Z"),
    currentTime: new Date("2026-08-26T10:00:00Z"),
    hourOfDay: 15,
    dayOfWeek: 2,
    isRetryable: true,
  });
  assert.equal(action.action, "retry_now");
  assert.equal(calls, 2);
  assert.equal(agent.fallbackCount, 0);
});

test("PolicyAgent: fatal 402 sets lastErrorStatus and falls back", async () => {
  const client = {
    async complete(): Promise<string> {
      throw new LlmError("402 payment required", 402);
    },
  };
  const agent = new PolicyAgent(settings(), client, async () => {});
  const action = await agent.decide({
    paymentId: "pay_T",
    failureClass: FailureClass.InsufficientFunds,
    errorCode: "BAD_REQUEST_ERROR",
    amount: 10000,
    currency: "INR",
    method: "upi",
    retryCount24h: 0,
    nudgeCount24h: 0,
    previousRetryOutcomes: [],
    failedAt: new Date("2026-08-26T09:00:00Z"),
    currentTime: new Date("2026-08-26T10:00:00Z"),
    hourOfDay: 15,
    dayOfWeek: 2,
    isRetryable: true,
  });
  assert.equal(action.action, "abandon");
  assert.equal(agent.fallbackCount, 1);
  assert.equal(agent.lastErrorStatus, 402);
});
