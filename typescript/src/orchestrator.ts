import { randomUUID as cryptoRandomUUID } from "node:crypto";
import type { FailureContext, RetryAction } from "./actions.js";
import { ClassifierMapper } from "./classifier.js";
import { GuardrailGate, type GuardrailResult } from "./gate.js";
import { NudgeGenerator } from "./nudgeGenerator.js";
import { RetryExecutor, type ExecutionResult } from "./executor.js";
import { resolveTargetRail } from "./railSelector.js";
import { clampRetryAtOutOfBlackout, istDayOfWeek, istHour } from "./time.js";
import {
  effectiveCounts,
  idempotencyKey as deriveIdempotencyKey,
  predictHeuristic,
} from "./policy.js";
import { isHardDecline, toFailureClass } from "./taxonomy.js";
import * as recoveryLink from "./recoveryLink.js";
import type { EngineSettings } from "./settings.js";
import type { RecoveryStore } from "./store.js";
import { UniqueViolation } from "./store.js";
import type { PaymentFailure, RecoveryCase, RetryAttempt, WebhookEvent } from "./entities.js";
import {
  attachAttemptToCase,
  caseStopReason,
  closeCase,
  logEvent,
  openCase,
} from "./cases.js";

export interface PolicyDecider {
  decide(context: FailureContext): Promise<RetryAction>;
  fallbackCount: number;
}

export interface OrchestratorDeps {
  store: RecoveryStore;
  settings: EngineSettings;
  classifier?: ClassifierMapper;
  guardrail?: GuardrailGate;
  nudgeGen?: NudgeGenerator;
  executor?: RetryExecutor;
  agent?: PolicyDecider | null;
  agentFactory?: () => PolicyDecider | null;
  xgboostPredict?: (context: FailureContext) => RetryAction;
}

export class PaymentRecoveryOrchestrator {
  private readonly store: RecoveryStore;
  private readonly settings: EngineSettings;
  private readonly classifier: ClassifierMapper;
  private readonly guardrail: GuardrailGate;
  private readonly nudgeGen: NudgeGenerator;
  private readonly executor: RetryExecutor;
  private agent: PolicyDecider | null | undefined;
  private readonly agentFactory?: () => PolicyDecider | null;
  private readonly xgboostPredict: (context: FailureContext) => RetryAction;

  constructor(deps: OrchestratorDeps) {
    this.store = deps.store;
    this.settings = deps.settings;
    this.classifier = deps.classifier ?? new ClassifierMapper();
    this.guardrail = deps.guardrail ?? new GuardrailGate(deps.settings);
    this.nudgeGen = deps.nudgeGen ?? new NudgeGenerator(deps.settings);
    this.executor =
      deps.executor ??
      new RetryExecutor({
        async createPaymentLink() {
          throw new Error("No Razorpay client configured");
        },
        async notifyBy() {
          throw new Error("No Razorpay client configured");
        },
      });
    this.agent = deps.agent;
    this.agentFactory = deps.agentFactory;
    this.xgboostPredict = deps.xgboostPredict ?? ((ctx) => predictHeuristic(ctx));
  }

  private getAgent(): PolicyDecider | null {
    if (this.agent === undefined) {
      this.agent = this.agentFactory ? this.agentFactory() : null;
    }
    return this.agent ?? null;
  }

  async processPaymentFailure(event: WebhookEvent, now: Date = new Date()): Promise<void> {
    const payload = event.payload as {
      payload?: { payment?: { entity?: Record<string, unknown> } };
    };
    const paymentEntity = payload.payload?.payment?.entity ?? {};
    if (Object.keys(paymentEntity).length === 0) return;

    const paymentId = String(paymentEntity["id"] ?? "unknown");

    const errorCode = String(paymentEntity["error_code"] ?? "UNKNOWN");
    const errorDesc = (paymentEntity["error_description"] as string) ?? null;
    const errorSource = (paymentEntity["error_source"] as string) ?? null;
    const errorStep = (paymentEntity["error_step"] as string) ?? null;
    const errorReason = (paymentEntity["error_reason"] as string) ?? null;

    const { failureClass, retryable } = this.classifier.classify(
      errorCode,
      errorDesc,
      errorSource,
      errorStep,
      errorReason,
    );

    const cardInfo = (paymentEntity["card"] as Record<string, unknown> | null) ?? {};
    const failureRecord: PaymentFailure = {
      id: cryptoRandomUUID(),
      paymentId,
      orderId: (paymentEntity["order_id"] as string) ?? null,
      amount: Number(paymentEntity["amount"] ?? 0),
      currency: String(paymentEntity["currency"] ?? "INR"),
      method: String(paymentEntity["method"] ?? "unknown"),
      bank: (paymentEntity["bank"] as string) ?? null,
      wallet: (paymentEntity["wallet"] as string) ?? null,
      vpa: (paymentEntity["vpa"] as string) ?? null,
      cardNetwork: (cardInfo["network"] as string) ?? null,
      cardType: (cardInfo["type"] as string) ?? null,
      cardIssuer: (cardInfo["issuer"] as string) ?? null,
      errorCode,
      errorDescription: errorDesc,
      errorSource,
      errorStep,
      errorReason,
      failureClass,
      isRetryable: retryable,
      customerEmail: (paymentEntity["email"] as string) ?? null,
      customerContact: (paymentEntity["contact"] as string) ?? null,
      webhookEventId: event.id,
      failedAt: new Date(Number(paymentEntity["created_at"] ?? 0) * 1000),
      createdAt: now,
    };
    await this.store.insertFailure(failureRecord);

    const caseRecord = await openCase(
      this.store,
      this.settings,
      {
        riskType: "payment_failure",
        subjectRef: paymentId,
        amountAtRisk: failureRecord.amount,
        currency: failureRecord.currency,
        customerId: failureRecord.customerEmail ?? failureRecord.customerContact,
        batchId: now.toISOString().slice(0, 10),
      },
      now,
    );

    if (isHardDeclineClass(failureClass)) {
      closeCase(caseRecord, "abandoned", `hard decline: ${failureClass}`, now);
      this.store.saveCase(caseRecord);
      await logEvent(this.store, caseRecord, "closed", "deterministic", {
        state: "abandoned",
        reason: `hard decline: ${failureClass}`,
        error_code: errorCode,
      });

      const abandonKey = `abandon_${paymentId}`;
      if (await this.store.findAttemptByIdempotencyKey(abandonKey)) {
        return;
      }
      const attempt: RetryAttempt = blankAttempt(now);
      attempt.paymentFailureId = failureRecord.id;
      attempt.paymentId = paymentId;
      attempt.idempotencyKey = abandonKey;
      attempt.attemptNumber = 0;
      attempt.actionType = "abandon";
      attempt.agentReasoning = `Hard decline: ${failureClass}`;
      attempt.agentType = "deterministic";
      attempt.guardrailPassed = true;
      attempt.result = "skipped";
      attempt.recoveryCaseId = caseRecord.id;
      await this.store.insertAttempt(attempt);
      return;
    }

    const ledger = await this.getLedger(caseRecord.customerId);
    const stop = caseStopReason(caseRecord, ledger?.consentStatus ?? null, now);
    if (stop !== null) {
      await logEvent(
        this.store,
        caseRecord,
        stop.startsWith("next action not due") ? "deferred" : "stopped",
        "system",
        {
          reason: stop,
          attempts_used: caseRecord.attemptsUsed,
          max_attempts: caseRecord.maxAttempts,
        },
      );
      return;
    }

    const context = await this.buildFailureContext(failureRecord, now);

    let action: RetryAction | null = null;
    let agentType = "xgboost";
    const agent = this.getAgent();
    if (agent !== null) {
      const fallbacksBefore = agent.fallbackCount;
      try {
        const candidate = await agent.decide(context);
        if (agent.fallbackCount === fallbacksBefore) {
          action = candidate;
          agentType = "llm";
        }
      } catch {
        action = null;
      }
    }
    if (action === null) {
      action = this.xgboostPredict(context);
      agentType = "xgboost";
    }

    if (action.action === "switch_rail") {
      const resolved = resolveTargetRail(context.method, action.rail, context.failureClass);
      if (resolved !== action.rail) {
        action.rail = resolved;
      }
    }

    if (action.action === "retry_at" && action.retryAt != null) {
      action.retryAt = clampRetryAtOutOfBlackout(action.retryAt, this.settings);
    }

    const attemptCount = await this.store.countAttemptsByPaymentId(paymentId);
    const idemKey = deriveIdempotencyKey(paymentId, attemptCount);

    if (await this.store.findAttemptByIdempotencyKey(idemKey)) {
      return;
    }

    const guardrailResult = this.guardrail.validate(
      action,
      context,
      idemKey,
      attemptCount,
      now,
    );

    const attempt: RetryAttempt = blankAttempt(now);
    attempt.paymentFailureId = failureRecord.id;
    attempt.paymentId = paymentId;
    attempt.idempotencyKey = idemKey;
    attempt.attemptNumber = attemptCount + 1;
    attempt.actionType = action.action;
    attempt.targetRail = action.rail ?? null;
    attempt.scheduledAt = action.retryAt ? action.retryAt : null;
    attempt.agentReasoning = action.reason;
    attempt.agentType = agentType;
    attempt.agentConfidence = action.confidence ?? null;
    attempt.guardrailPassed = guardrailResult.passed;
    attempt.guardrailRejectionReason = guardrailResult.rejectionReasons.length
      ? guardrailResult.rejectionReasons.join("; ")
      : null;

    attachAttemptToCase(caseRecord, attempt, this.settings.escalationBackoffHours, now);
    this.store.saveCase(caseRecord);

    if (guardrailResult.passed) {
      if (action.action === "retry_at" && action.retryAt != null) {
        attempt.result = "scheduled";
        await this.store.insertAttempt(attempt);
        this.store.saveCase(caseRecord);
        await logEvent(this.store, caseRecord, "deferred", agentType, {
          action: "retry_at",
          scheduled_at: action.retryAt.toISOString(),
          reason: action.reason,
        });
        await this.updateLedgerAndCommit(context.customerId, action);
        return;
      }

      await this.executeAndRecord(
        attempt,
        caseRecord,
        failureRecord,
        action,
        idemKey,
        agentType,
        now,
      );
      await this.updateLedgerAndCommit(context.customerId, action);
      return;
    }

    attempt.result = "rejected";
    await this.store.insertAttempt(attempt);
    this.store.saveCase(caseRecord);
  }

  async executeAndRecord(
    attempt: RetryAttempt,
    kase: RecoveryCase,
    failureRecord: PaymentFailure,
    action: RetryAction,
    idemKey: string,
    actor: string,
    now: Date = new Date(),
  ): Promise<void> {
    let nudgeMessage: string | null = null;

    if (action.action === "nudge_customer" || action.action === "switch_rail") {
      try {
        const page = recoveryLink.urlFor(
          kase.id,
          this.settings.recoveryLinkSecret,
          this.settings.publicBaseUrl,
          this.settings.consentWindowHours,
          now,
        );
        const nextStep = page
          ? `Check your payment and pay securely here: ${page}`
          : "Please try again using a different payment method.";
        nudgeMessage = await this.nudgeGen.generate(
          failureRecord.failureClass,
          failureRecord.amount,
          failureRecord.method,
          nextStep,
          null,
        );
        attempt.nudgeMessage = nudgeMessage;
      } catch {
        /* proceed without nudge */
      }
    }

    if (action.action === "abandon") {
      attempt.result = "skipped";
      return;
    }

    attempt.result = "pending";
    try {
      await this.store.insertAttempt(attempt);
    } catch (e) {
      if (e instanceof UniqueViolation) {
        const existing = await this.store.findAttemptByIdempotencyKey(idemKey);
        if (existing !== null && existing.id === attempt.id) {
          this.store.saveAttempt(attempt);
        } else {
          return;
        }
      } else {
        throw e;
      }
    }

    let execResult: ExecutionResult;
    try {
      execResult = await this.executor.executeRetry(
        failureRecord,
        action.action,
        action.rail,
        idemKey,
        nudgeMessage,
      );
    } catch (e) {
      execResult = { success: false, error: String(e) };
    }

    attempt.executedAt = new Date();
    attempt.result = execResult.success ? "success" : "failed";
    attempt.resultDetails = execResult as unknown as Record<string, unknown>;
    if (execResult.paymentLinkId) {
      attempt.externalRef = execResult.paymentLinkId;
    }
    if (nudgeMessage) {
      attempt.nudgeSent = execResult.nudgeSent ?? false;
    }
    const channels = execResult.channels ?? [];
    attempt.channel = channels.length ? channels[0] : "payment_link";
    this.store.saveAttempt(attempt);

    const freshCase = (await this.store.getCase(kase.id)) ?? kase;
    if (attempt.result === "success" && action.action === "nudge_customer") {
      await logEvent(this.store, freshCase, "escalated", actor, {
        level: freshCase.escalationLevel,
        channel: attempt.channel,
        next_action_at: freshCase.nextActionAt?.toISOString() ?? null,
      });
    } else if (attempt.result === "success") {
      await logEvent(this.store, freshCase, "contacted", actor, {
        action: action.action,
        channel: attempt.channel,
        external_ref: attempt.externalRef ?? null,
      });
    }
  }

  async buildFailureContext(
    failure: PaymentFailure,
    now: Date = new Date(),
  ): Promise<FailureContext> {
    const customerId = failure.customerEmail ?? failure.customerContact;

    const previousOutcomes = await this.store.listRecentAttemptResultsByPaymentId(
      failure.paymentId,
      5,
    );

    let retryCount = 0;
    let nudgeCount = 0;
    if (customerId) {
      const ledgerRow = await this.getLedger(customerId);
      if (ledgerRow) {
        const counts = effectiveCounts(
          {
            totalRetries24h: ledgerRow.totalRetries24h,
            totalNudges24h: ledgerRow.totalNudges24h,
            lastRetryAt: ledgerRow.lastRetryAt ?? null,
            lastNudgeAt: ledgerRow.lastNudgeAt ?? null,
          },
          now,
          this.settings.rateLimitWindowHours,
        );
        retryCount = counts.retries;
        nudgeCount = counts.nudges;
      }
    }

    return {
      paymentId: failure.paymentId,
      orderId: failure.orderId,
      failureClass: failure.failureClass,
      errorCode: failure.errorCode,
      errorDescription: failure.errorDescription,
      errorSource: failure.errorSource,
      errorReason: failure.errorReason,
      amount: failure.amount,
      currency: failure.currency,
      method: failure.method,
      bank: failure.bank ?? failure.cardIssuer,
      cardNetwork: failure.cardNetwork,
      cardType: failure.cardType,
      customerId,
      customerEmail: failure.customerEmail,
      customerContact: failure.customerContact,
      retryCount24h: retryCount,
      nudgeCount24h: nudgeCount,
      previousRetryOutcomes: previousOutcomes,
      failedAt: failure.failedAt,
      currentTime: now,
      hourOfDay: istHour(now),
      dayOfWeek: istDayOfWeek(now),
      isRetryable: failure.isRetryable,
      originalFailureId: failure.id,
    };
  }

  async getLedger(customerId: string | null | undefined) {
    if (!customerId) return null;
    return this.store.getLedger(customerId);
  }

  private async updateLedgerAndCommit(
    customerId: string | null | undefined,
    action: RetryAction,
  ): Promise<void> {
    if (!customerId) return;
    const now = new Date();
    const windowMs = this.settings.rateLimitWindowHours * 3_600_000;

    let ledger = await this.store.getLedger(customerId);
    if (ledger === null) {
      ledger = {
        id: 0,
        customerId,
        totalRetries24h: 0,
        totalNudges24h: 0,
        consentStatus: "granted",
        updatedAt: now,
      };
      await this.store.insertLedger(ledger);
    }

    if (action.action === "retry_now" || action.action === "retry_at" || action.action === "switch_rail") {
      const last = ledger.lastRetryAt ?? null;
      if (last === null || now.getTime() - last.getTime() > windowMs) {
        ledger.totalRetries24h = 0;
      }
      ledger.totalRetries24h = (ledger.totalRetries24h || 0) + 1;
      ledger.lastRetryAt = now;
    }

    if (action.action === "nudge_customer") {
      const last = ledger.lastNudgeAt ?? null;
      if (last === null || now.getTime() - last.getTime() > windowMs) {
        ledger.totalNudges24h = 0;
      }
      ledger.totalNudges24h = (ledger.totalNudges24h || 0) + 1;
      ledger.lastNudgeAt = now;
    }

    ledger.updatedAt = now;
    this.store.saveLedger(ledger);
  }
}

function blankAttempt(now: Date): RetryAttempt {
  return {
    id: cryptoRandomUUID(),
    paymentFailureId: null,
    paymentId: null,
    idempotencyKey: "",
    attemptNumber: 0,
    recoveryCaseId: null,
    externalRef: null,
    actionType: "",
    targetRail: null,
    scheduledAt: null,
    agentReasoning: null,
    agentType: null,
    agentConfidence: null,
    guardrailPassed: false,
    guardrailRejectionReason: null,
    executedAt: null,
    result: null,
    resultDetails: null,
    nudgeMessage: null,
    nudgeSent: false,
    channel: null,
    language: null,
    createdAt: now,
  };
}

function isHardDeclineClass(fc: string): boolean {
  const parsed = toFailureClass(fc);
  return parsed !== null && isHardDecline(parsed);
}

export type { GuardrailResult };
