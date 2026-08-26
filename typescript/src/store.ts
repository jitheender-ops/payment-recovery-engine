import type {
  CaseEvent,
  CaseEventType,
  PaymentFailure,
  ProcessedEvent,
  PromiseToPay,
  RecoveryCase,
  RetryAttempt,
  RetryLedger,
  RiskType,
  WebhookEvent,
} from "./entities.js";

export class UniqueViolation extends Error {
  constructor(public readonly constraint: string) {
    super(`unique constraint violated: ${constraint}`);
    this.name = "UniqueViolation";
  }
}

export interface ClaimedAttempt {
  attempt: RetryAttempt;
  claimed: boolean;
}

export interface ClaimedEvent {
  event: WebhookEvent;
  claimed: boolean;
}

export interface StalePendingUpdate {
  result: "failed";
  executedAt: Date;
  resultDetails: Record<string, unknown>;
}

export interface RecoveryStore {
  insertEvent(event: WebhookEvent): Promise<void>;
  getEventByRazorpayId(razorpayEventId: string): Promise<WebhookEvent | null>;
  claimEvent(id: string): Promise<ClaimedEvent>;
  listStaleUnprocessedEvents(cutoff: Date, limit: number): Promise<WebhookEvent[]>;
  markEventProcessed(razorpayEventId: string): Promise<void>;
  markEventError(razorpayEventId: string, error: string): Promise<void>;

  recordProcessedEvent(razorpayEventId: string): Promise<boolean>;

  insertFailure(failure: PaymentFailure): Promise<void>;
  getFailure(id: string): Promise<PaymentFailure | null>;
  findOpenCaseIdByOrder(orderRef: string): Promise<string | null>;

  findCase(riskType: RiskType, subjectRef: string): Promise<RecoveryCase | null>;
  insertCase(kase: RecoveryCase): Promise<void>;
  getCase(id: string): Promise<RecoveryCase | null>;
  saveCase(kase: RecoveryCase): void;
  listOpenCasesByCustomer(customerId: string): Promise<RecoveryCase[]>;
  listDueCases(now: Date, riskType: string | null, limit: number): Promise<RecoveryCase[]>;

  findAttemptByIdempotencyKey(key: string): Promise<RetryAttempt | null>;
  findNewestAttemptByExternalRef(externalRef: string): Promise<RetryAttempt | null>;
  countAttemptsByPaymentId(paymentId: string): Promise<number>;
  listRecentAttemptResultsByPaymentId(paymentId: string, limit: number): Promise<string[]>;
  insertAttempt(attempt: RetryAttempt): Promise<void>;
  getAttempt(id: string): Promise<RetryAttempt | null>;
  saveAttempt(attempt: RetryAttempt): void;
  listDueScheduledAttempts(now: Date, limit: number): Promise<RetryAttempt[]>;
  claimScheduledAttempt(id: string): Promise<ClaimedAttempt>;
  listStalePendingAttempts(cutoff: Date, limit: number): Promise<RetryAttempt[]>;
  claimStalePendingAttempt(
    id: string,
    update: StalePendingUpdate,
  ): Promise<ClaimedAttempt>;
  supersedePendingAttemptsByCase(caseId: string, now: Date): Promise<number>;

  getLedger(customerId: string): Promise<RetryLedger | null>;
  insertLedger(ledger: RetryLedger): Promise<void>;
  saveLedger(ledger: RetryLedger): void;

  insertPromise(promise: PromiseToPay): Promise<void>;
  listPendingPromisesByCase(caseId: string): Promise<PromiseToPay[]>;
  listDuePromises(now: Date, limit: number): Promise<PromiseToPay[]>;
  savePromise(promise: PromiseToPay): void;

  insertCaseEvent(event: CaseEvent): Promise<void>;
  listCaseEvents(caseId: string): Promise<CaseEvent[]>;

  summarizeBatch(
    batchId: string | null,
  ): Promise<{
    cases: number;
    atRiskPaise: number;
    recoveredPaise: number;
    attributedPaise: number;
    attemptsUsed: number;
  }>;
}

export class InMemoryStore implements RecoveryStore {
  private events = new Map<string, WebhookEvent>();
  private processedEvents = new Map<string, ProcessedEvent>();
  private processedSeq = 0;
  private failures = new Map<string, PaymentFailure>();
  private cases = new Map<string, RecoveryCase>();
  private attempts = new Map<string, RetryAttempt>();
  private ledgers = new Map<string, RetryLedger>();
  private promises = new Map<string, PromiseToPay>();
  private caseEvents: CaseEvent[] = [];
  private caseEventSeq = 0;
  private processedEventSeq = 0;

  async insertEvent(event: WebhookEvent): Promise<void> {
    if (this.events.has(event.razorpayEventId)) {
      throw new UniqueViolation("uq_webhook_event_id");
    }
    this.events.set(event.razorpayEventId, { ...event });
  }

  async getEventByRazorpayId(razorpayEventId: string): Promise<WebhookEvent | null> {
    return this.events.get(razorpayEventId) ?? null;
  }

  async claimEvent(id: string): Promise<ClaimedEvent> {
    for (const event of this.events.values()) {
      if (event.id !== id) continue;
      if (event.processed) return { event, claimed: false };
      event.processed = true;
      return { event, claimed: true };
    }
    return { event: null as unknown as WebhookEvent, claimed: false };
  }

  async listStaleUnprocessedEvents(cutoff: Date, limit: number): Promise<WebhookEvent[]> {
    return [...this.events.values()]
      .filter(
        (e) =>
          !e.processed &&
          e.eventType === "payment.failed" &&
          e.receivedAt <= cutoff,
      )
      .sort((a, b) => a.receivedAt.getTime() - b.receivedAt.getTime())
      .slice(0, limit);
  }

  async markEventProcessed(razorpayEventId: string): Promise<void> {
    const event = this.events.get(razorpayEventId);
    if (event) event.processed = true;
  }

  async markEventError(razorpayEventId: string, error: string): Promise<void> {
    const event = this.events.get(razorpayEventId);
    if (event) {
      event.processed = true;
      event.processingError = error;
    }
  }

  async recordProcessedEvent(razorpayEventId: string): Promise<boolean> {
    if (this.processedEvents.has(razorpayEventId)) return false;
    this.processedEvents.set(razorpayEventId, {
      id: ++this.processedEventSeq,
      razorpayEventId,
      processedAt: new Date(),
    });
    return true;
  }

  async insertFailure(failure: PaymentFailure): Promise<void> {
    this.failures.set(failure.id, { ...failure });
  }

  async getFailure(id: string): Promise<PaymentFailure | null> {
    return this.failures.get(id) ?? null;
  }

  async findOpenCaseIdByOrder(orderRef: string): Promise<string | null> {
    let newest: RecoveryCase | null = null;
    for (const failure of this.failures.values()) {
      if (failure.orderId !== orderRef) continue;
      const kase = this.cases.get(`payment_failure:${failure.paymentId}`);
      if (kase && kase.state === "open") {
        if (newest === null || kase.openedAt > newest.openedAt) newest = kase;
      }
    }
    return newest?.id ?? null;
  }

  async findCase(riskType: RiskType, subjectRef: string): Promise<RecoveryCase | null> {
    return this.cases.get(`${riskType}:${subjectRef}`) ?? null;
  }

  async insertCase(kase: RecoveryCase): Promise<void> {
    const key = `${kase.riskType}:${kase.subjectRef}`;
    if (this.cases.has(key)) throw new UniqueViolation("uq_recovery_case_subject");
    this.cases.set(key, { ...kase });
  }

  async getCase(id: string): Promise<RecoveryCase | null> {
    for (const kase of this.cases.values()) if (kase.id === id) return { ...kase };
    return null;
  }

  private putCase(kase: RecoveryCase): void {
    this.cases.set(`${kase.riskType}:${kase.subjectRef}`, { ...kase });
  }

  async listOpenCasesByCustomer(customerId: string): Promise<RecoveryCase[]> {
    return [...this.cases.values()]
      .filter((c) => c.customerId === customerId && c.state === "open")
      .map((c) => ({ ...c }));
  }

  async listDueCases(now: Date, riskType: string | null, limit: number): Promise<RecoveryCase[]> {
    return [...this.cases.values()]
      .filter(
        (c) =>
          c.state === "open" &&
          c.nextActionAt != null &&
          c.nextActionAt <= now &&
          (riskType === null || c.riskType === riskType),
      )
      .sort((a, b) => (a.nextActionAt?.getTime() ?? 0) - (b.nextActionAt?.getTime() ?? 0))
      .slice(0, limit)
      .map((c) => ({ ...c }));
  }

  async findAttemptByIdempotencyKey(key: string): Promise<RetryAttempt | null> {
    for (const a of this.attempts.values()) {
      if (a.idempotencyKey === key) return { ...a };
    }
    return null;
  }

  async findNewestAttemptByExternalRef(externalRef: string): Promise<RetryAttempt | null> {
    let newest: RetryAttempt | null = null;
    for (const a of this.attempts.values()) {
      if (a.externalRef !== externalRef) continue;
      if (newest === null || a.createdAt > newest.createdAt) newest = a;
    }
    return newest ? { ...newest } : null;
  }

  async countAttemptsByPaymentId(paymentId: string): Promise<number> {
    let n = 0;
    for (const a of this.attempts.values()) if (a.paymentId === paymentId) n++;
    return n;
  }

  async listRecentAttemptResultsByPaymentId(paymentId: string, limit: number): Promise<string[]> {
    return [...this.attempts.values()]
      .filter((a) => a.paymentId === paymentId)
      .sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime())
      .slice(0, limit)
      .map((a) => a.result as string)
      .filter((r): r is string => r != null);
  }

  async insertAttempt(attempt: RetryAttempt): Promise<void> {
    for (const a of this.attempts.values()) {
      if (a.idempotencyKey === attempt.idempotencyKey) {
        throw new UniqueViolation("uq_retry_attempts_idempotency_key");
      }
    }
    this.attempts.set(attempt.id, { ...attempt });
  }

  async getAttempt(id: string): Promise<RetryAttempt | null> {
    return this.attempts.get(id) ?? null;
  }

  private putAttempt(attempt: RetryAttempt): void {
    this.attempts.set(attempt.id, { ...attempt });
  }

  async listDueScheduledAttempts(now: Date, limit: number): Promise<RetryAttempt[]> {
    return [...this.attempts.values()]
      .filter(
        (a) => a.result === "scheduled" && a.scheduledAt != null && a.scheduledAt <= now,
      )
      .sort((a, b) => (a.scheduledAt?.getTime() ?? 0) - (b.scheduledAt?.getTime() ?? 0))
      .slice(0, limit)
      .map((a) => ({ ...a }));
  }

  async claimScheduledAttempt(id: string): Promise<ClaimedAttempt> {
    const attempt = this.attempts.get(id);
    if (!attempt) return { attempt: null as unknown as RetryAttempt, claimed: false };
    if (attempt.result !== "scheduled") return { attempt: { ...attempt }, claimed: false };
    attempt.result = "pending";
    this.putAttempt(attempt);
    return { attempt: { ...attempt }, claimed: true };
  }

  async listStalePendingAttempts(cutoff: Date, limit: number): Promise<RetryAttempt[]> {
    return [...this.attempts.values()]
      .filter((a) => a.result === "pending" && a.createdAt <= cutoff)
      .sort((a, b) => a.createdAt.getTime() - b.createdAt.getTime())
      .slice(0, limit)
      .map((a) => ({ ...a }));
  }

  async claimStalePendingAttempt(
    id: string,
    update: StalePendingUpdate,
  ): Promise<ClaimedAttempt> {
    const attempt = this.attempts.get(id);
    if (!attempt) return { attempt: null as unknown as RetryAttempt, claimed: false };
    if (attempt.result !== "pending") return { attempt: { ...attempt }, claimed: false };
    attempt.result = update.result;
    attempt.executedAt = update.executedAt;
    attempt.resultDetails = update.resultDetails;
    this.putAttempt(attempt);
    return { attempt: { ...attempt }, claimed: true };
  }

  async supersedePendingAttemptsByCase(caseId: string, now: Date): Promise<number> {
    let n = 0;
    for (const a of this.attempts.values()) {
      if (a.recoveryCaseId === caseId && a.result === "pending") {
        a.result = "superseded";
        a.executedAt = now;
        this.putAttempt(a);
        n++;
      }
    }
    return n;
  }

  async getLedger(customerId: string): Promise<RetryLedger | null> {
    const l = this.ledgers.get(customerId);
    return l ? { ...l } : null;
  }

  async insertLedger(ledger: RetryLedger): Promise<void> {
    this.ledgers.set(ledger.customerId, { ...ledger });
  }

  saveLedger(ledger: RetryLedger): void {
    this.ledgers.set(ledger.customerId, { ...ledger });
  }

  async insertPromise(promise: PromiseToPay): Promise<void> {
    this.promises.set(promise.id, { ...promise });
  }

  async listPendingPromisesByCase(caseId: string): Promise<PromiseToPay[]> {
    return [...this.promises.values()]
      .filter((p) => p.recoveryCaseId === caseId && p.status === "pending")
      .map((p) => ({ ...p }));
  }

  async listDuePromises(now: Date, limit: number): Promise<PromiseToPay[]> {
    return [...this.promises.values()]
      .filter((p) => p.status === "pending" && p.dueAt <= now)
      .sort((a, b) => a.dueAt.getTime() - b.dueAt.getTime())
      .slice(0, limit)
      .map((p) => ({ ...p }));
  }

  savePromise(promise: PromiseToPay): void {
    this.promises.set(promise.id, { ...promise });
  }

  async insertCaseEvent(event: CaseEvent): Promise<void> {
    this.caseEvents.push({ ...event, id: ++this.caseEventSeq });
  }

  async listCaseEvents(caseId: string): Promise<CaseEvent[]> {
    return this.caseEvents
      .filter((e) => e.recoveryCaseId === caseId)
      .sort((a, b) => a.id - b.id)
      .map((e) => ({ ...e }));
  }

  async summarizeBatch(batchId: string | null): Promise<{
    cases: number;
    atRiskPaise: number;
    recoveredPaise: number;
    attributedPaise: number;
    attemptsUsed: number;
  }> {
    let cases = 0;
    let atRisk = 0;
    let recovered = 0;
    let attributed = 0;
    let attemptsUsed = 0;
    for (const c of this.cases.values()) {
      if (batchId !== null && c.batchId !== batchId) continue;
      cases++;
      atRisk += c.amountAtRisk;
      recovered += c.amountRecovered;
      if (c.recoveredViaAttemptId != null) attributed += c.amountRecovered;
      attemptsUsed += c.attemptsUsed;
    }
    return { cases, atRiskPaise: atRisk, recoveredPaise: recovered, attributedPaise: attributed, attemptsUsed };
  }

  saveCase(kase: RecoveryCase): void {
    this.putCase(kase);
  }

  saveAttempt(attempt: RetryAttempt): void {
    this.putAttempt(attempt);
  }

  findCaseByKey(riskType: string, subjectRef: string): RecoveryCase | null {
    const c = this.cases.get(`${riskType}:${subjectRef}`);
    return c ? { ...c } : null;
  }
}

export type { CaseEventType };
