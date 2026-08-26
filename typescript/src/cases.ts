import { randomUUID } from "node:crypto";
import { TERMINAL_CASE_STATES, PROMISE_RESOLUTION_EVENT } from "./entities.js";
import type {
  CaseEvent,
  CaseEventType,
  Channel,
  PromiseResolution,
  PromiseToPay,
  RecoveryCase,
  RetryAttempt,
  RiskType,
} from "./entities.js";
import type { RecoveryStore } from "./store.js";
import { UniqueViolation } from "./store.js";
import type { EngineSettings } from "./settings.js";
import { stopReason as computeStopReason } from "./policy.js";

export interface OpenCaseOptions {
  riskType: RiskType;
  subjectRef: string;
  amountAtRisk: number;
  customerId?: string | null;
  batchId?: string | null;
  currency?: string;
  maxAttempts?: number | null;
  dueAt?: Date | null;
  nextActionAt?: Date | null;
}

export async function openCase(
  store: RecoveryStore,
  settings: EngineSettings,
  opts: OpenCaseOptions,
  now: Date = new Date(),
): Promise<RecoveryCase> {
  const existing = await store.findCase(opts.riskType, opts.subjectRef);
  if (existing !== null) return existing;

  const kase: RecoveryCase = {
    id: randomUUID(),
    riskType: opts.riskType,
    subjectRef: opts.subjectRef,
    customerId: opts.customerId ?? null,
    amountAtRisk: opts.amountAtRisk,
    currency: opts.currency ?? "INR",
    amountRecovered: 0,
    dueAt: opts.dueAt ?? null,
    nextActionAt: opts.nextActionAt ?? null,
    state: "open",
    attemptsUsed: 0,
    maxAttempts: opts.maxAttempts ?? settings.maxRetriesPerPayment,
    escalationLevel: 0,
    batchId: opts.batchId ?? null,
    openedAt: now,
    updatedAt: now,
  };

  try {
    await store.insertCase(kase);
  } catch (e) {
    if (e instanceof UniqueViolation) {
      const won = await store.findCase(opts.riskType, opts.subjectRef);
      if (won === null) throw e;
      return won;
    }
    throw e;
  }

  await logEvent(store, kase, "opened", "system", {
    risk_type: kase.riskType,
    subject_ref: kase.subjectRef,
    amount_at_risk: kase.amountAtRisk,
    max_attempts: kase.maxAttempts,
  });
  return kase;
}

export async function findCase(
  store: RecoveryStore,
  riskType: RiskType,
  subjectRef: string,
): Promise<RecoveryCase | null> {
  return store.findCase(riskType, subjectRef);
}

export function caseStopReason(
  kase: RecoveryCase,
  ledgerConsent: string | null | undefined,
  now: Date = new Date(),
): string | null {
  return computeStopReason(
    {
      state: kase.state,
      attemptsUsed: kase.attemptsUsed,
      maxAttempts: kase.maxAttempts,
      closeReason: kase.closeReason,
      nextActionAt: kase.nextActionAt,
    },
    ledgerConsent != null ? { consentStatus: ledgerConsent } : null,
    now,
  );
}

export function attachAttemptToCase(
  kase: RecoveryCase,
  attempt: RetryAttempt,
  escalationBackoffHours: number,
  now: Date = new Date(),
): void {
  attempt.recoveryCaseId = kase.id;
  kase.attemptsUsed += 1;
  if (attempt.actionType === "nudge_customer") {
    kase.escalationLevel += 1;
    kase.nextActionAt = new Date(
      now.getTime() + escalationBackoffHours * 3_600_000 * kase.escalationLevel,
    );
  }
  kase.updatedAt = now;
}

export function closeCase(
  kase: RecoveryCase,
  state: RecoveryCase["state"],
  reason: string,
  now: Date = new Date(),
): void {
  if (TERMINAL_CASE_STATES.has(kase.state)) return;
  kase.state = state;
  kase.closeReason = reason;
  kase.closedAt = now;
  kase.updatedAt = now;
}

export async function logEvent(
  store: RecoveryStore,
  kase: RecoveryCase,
  eventType: CaseEventType,
  actor = "system",
  detail: Record<string, unknown> = {},
): Promise<void> {
  await store.insertCaseEvent({
    id: 0,
    recoveryCaseId: kase.id,
    eventType,
    actor,
    detail: Object.keys(detail).length ? detail : null,
    createdAt: new Date(),
  } satisfies CaseEvent);
}

export interface AttributeCaptureInput {
  amount: number;
  recoveredRef: string;
  linkId?: string | null;
  idempotencyKey?: string | null;
  orderRef?: string | null;
}

export async function attributeCapture(
  store: RecoveryStore,
  settings: EngineSettings,
  input: AttributeCaptureInput,
  now: Date = new Date(),
): Promise<RecoveryCase | null> {
  let attempt: RetryAttempt | null = null;
  if (input.linkId) {
    attempt = await store.findNewestAttemptByExternalRef(input.linkId);
  }
  if (attempt === null && input.idempotencyKey) {
    attempt = await store.findAttemptByIdempotencyKey(input.idempotencyKey);
  }

  let kase: RecoveryCase | null = null;
  if (attempt !== null && attempt.recoveryCaseId != null) {
    kase = await store.getCase(attempt.recoveryCaseId);
  } else if (input.orderRef) {
    const caseId = await store.findOpenCaseIdByOrder(input.orderRef);
    kase = caseId ? await store.getCase(caseId) : null;
    attempt = null;
  }

  if (kase === null) return null;

  kase.amountRecovered += input.amount;
  kase.recoveredRef = input.recoveredRef;
  kase.recoveredAt = now;
  kase.updatedAt = now;
  kase.recoveredViaAttemptId = attempt !== null ? attempt.id : null;
  store.saveCase(kase);

  await logEvent(store, kase, "attributed", "system", {
    amount: input.amount,
    recovered_ref: input.recoveredRef,
    attributed_to_attempt: attempt !== null ? attempt.id : null,
    matched_on: input.linkId
      ? "link_id"
      : input.idempotencyKey
        ? "idempotency_key"
        : "order_ref",
  });

  await resolvePromises(store, kase, "kept", input.recoveredRef, now);

  if (kase.amountRecovered >= kase.amountAtRisk) {
    closeCase(kase, "recovered", `captured ${kase.amountRecovered} paise via ${input.recoveredRef}`, now);
    store.saveCase(kase);
    await logEvent(store, kase, "closed", "system", {
      state: "recovered",
      reason: kase.closeReason,
    });
  }

  await store.supersedePendingAttemptsByCase(kase.id, now);
  return kase;
}

export async function recordOptOut(
  store: RecoveryStore,
  customerId: string,
  now: Date = new Date(),
): Promise<number> {
  let ledger = await store.getLedger(customerId);
  if (ledger === null) {
    ledger = {
      id: 0,
      customerId,
      totalRetries24h: 0,
      totalNudges24h: 0,
      consentStatus: "opted_out",
      optedOutAt: now,
      updatedAt: now,
    };
    await store.insertLedger(ledger);
  } else {
    ledger.consentStatus = "opted_out";
    ledger.optedOutAt = now;
    ledger.updatedAt = now;
    store.saveLedger(ledger);
  }

  const openCases = await store.listOpenCasesByCustomer(customerId);
  let closed = 0;
  for (const kase of openCases) {
    closeCase(kase, "opted_out", "customer withdrew consent", now);
    store.saveCase(kase);
    await resolvePromises(store, kase, "cancelled", null, now);
    await logEvent(store, kase, "opted_out", "customer", { closed_state: "opted_out" });
    closed++;
  }
  return closed;
}

export async function batchSummary(
  store: RecoveryStore,
  batchId: string | null = null,
): Promise<{
  cases: number;
  atRiskPaise: number;
  recoveredPaise: number;
  attributedPaise: number;
  attemptsUsed: number;
  recoveryRatePct: number;
  attributedRatePct: number;
}> {
  const s = await store.summarizeBatch(batchId);
  return {
    ...s,
    recoveryRatePct: s.atRiskPaise ? round2((s.recoveredPaise / s.atRiskPaise) * 100) : 0.0,
    attributedRatePct: s.atRiskPaise ? round2((s.attributedPaise / s.atRiskPaise) * 100) : 0.0,
  };
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

export interface RecordPromiseInput {
  amount: number;
  dueAt: Date;
  channel?: Channel | string | null;
  language?: string | null;
  sourceRef?: string | null;
  notes?: string | null;
}

export async function recordPromise(
  store: RecoveryStore,
  kase: RecoveryCase,
  input: RecordPromiseInput,
  now: Date = new Date(),
): Promise<PromiseToPay> {
  const promise: PromiseToPay = {
    id: randomUUID(),
    recoveryCaseId: kase.id,
    customerId: kase.customerId,
    amountPromised: input.amount,
    promisedAt: now,
    dueAt: input.dueAt,
    channel: input.channel ?? null,
    language: input.language ?? null,
    sourceRef: input.sourceRef ?? null,
    status: "pending",
    resolvedRef: null,
    notes: input.notes ?? null,
    createdAt: now,
  };
  await store.insertPromise(promise);

  kase.nextActionAt =
    kase.nextActionAt === null || kase.nextActionAt === undefined
      ? input.dueAt
      : kase.nextActionAt > input.dueAt
        ? kase.nextActionAt
        : input.dueAt;
  kase.updatedAt = now;
  store.saveCase(kase);

  await logEvent(store, kase, "promise_made", "customer", {
    promise_id: promise.id,
    amount: input.amount,
    due_at: input.dueAt.toISOString(),
    channel: input.channel ?? null,
  });
  return promise;
}

export async function resolvePromises(
  store: RecoveryStore,
  kase: RecoveryCase,
  status: PromiseResolution,
  ref: string | null,
  now: Date = new Date(),
): Promise<number> {
  const pending = await store.listPendingPromisesByCase(kase.id);
  let moved = 0;
  for (const promise of pending) {
    promise.status = status;
    promise.resolvedAt = now;
    promise.resolvedRef = ref;
    store.savePromise(promise);
    await logEvent(store, kase, PROMISE_RESOLUTION_EVENT[status], "system", {
      promise_id: promise.id,
      amount: promise.amountPromised,
      due_at: promise.dueAt.toISOString(),
      resolved_ref: ref,
    });
    moved++;
  }
  return moved;
}

export async function expirePromises(
  store: RecoveryStore,
  settings: EngineSettings,
  now: Date = new Date(),
  limit = 500,
): Promise<number> {
  const due = await store.listDuePromises(now, limit);
  let broken = 0;
  for (const promise of due) {
    promise.status = "broken";
    promise.resolvedAt = now;
    store.savePromise(promise);

    const kase = await store.getCase(promise.recoveryCaseId);
    if (kase === null) continue;
    if (kase.state === "open") {
      kase.nextActionAt = now;
      kase.updatedAt = now;
      store.saveCase(kase);
    }
    await logEvent(store, kase, "promise_broken", "system", {
      promise_id: promise.id,
      amount: promise.amountPromised,
      due_at: promise.dueAt.toISOString(),
      case_state: kase.state,
    });
    broken++;
  }
  void settings;
  return broken;
}

export async function dueCases(
  store: RecoveryStore,
  now: Date = new Date(),
  riskType: string | null = null,
  limit = 100,
): Promise<RecoveryCase[]> {
  return store.listDueCases(now, riskType, limit);
}
