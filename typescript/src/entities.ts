export interface WebhookEvent {
  id: string;
  razorpayEventId: string;
  eventType: string;
  payload: Record<string, unknown>;
  receivedAt: Date;
  processed: boolean;
  processingError?: string | null;
}

export interface ProcessedEvent {
  id: number;
  razorpayEventId: string;
  processedAt: Date;
}

export interface PaymentFailure {
  id: string;
  paymentId: string;
  orderId?: string | null;
  amount: number;
  currency: string;
  method: string;
  bank?: string | null;
  wallet?: string | null;
  vpa?: string | null;
  cardNetwork?: string | null;
  cardType?: string | null;
  cardIssuer?: string | null;
  errorCode: string;
  errorDescription?: string | null;
  errorSource?: string | null;
  errorStep?: string | null;
  errorReason?: string | null;
  failureClass: string;
  isRetryable: boolean;
  customerEmail?: string | null;
  customerContact?: string | null;
  webhookEventId: string;
  failedAt: Date;
  createdAt: Date;
}

export type RiskType =
  | "payment_failure"
  | "checkout_abandonment"
  | "subscription_failure"
  | "invoice_overdue"
  | "mandate_failure";

export type CaseState =
  | "open"
  | "recovered"
  | "exhausted"
  | "abandoned"
  | "expired"
  | "opted_out";

export type Channel = "whatsapp" | "sms" | "email" | "voice" | "payment_link";

export type PromiseStatus = "pending" | "kept" | "broken" | "cancelled";

export type PromiseResolution = "kept" | "broken" | "cancelled";

export type CaseEventType =
  | "opened"
  | "contacted"
  | "escalated"
  | "promise_made"
  | "promise_kept"
  | "promise_broken"
  | "promise_cancelled"
  | "attributed"
  | "closed"
  | "opted_out"
  | "deferred"
  | "stopped"
  | "reconciled";

export const TERMINAL_CASE_STATES: ReadonlySet<string> = new Set([
  "recovered",
  "exhausted",
  "abandoned",
  "expired",
  "opted_out",
]);

export interface RecoveryCase {
  id: string;
  riskType: RiskType;
  subjectRef: string;
  customerId?: string | null;
  amountAtRisk: number;
  currency: string;
  amountRecovered: number;
  recoveredRef?: string | null;
  recoveredAt?: Date | null;
  recoveredViaAttemptId?: string | null;
  dueAt?: Date | null;
  nextActionAt?: Date | null;
  state: CaseState;
  closeReason?: string | null;
  closedAt?: Date | null;
  attemptsUsed: number;
  maxAttempts: number;
  escalationLevel: number;
  batchId?: string | null;
  openedAt: Date;
  updatedAt: Date;
}

export type AttemptResult =
  | "pending"
  | "scheduled"
  | "success"
  | "failed"
  | "rejected"
  | "skipped"
  | "superseded"
  | "cancelled";

export interface RetryAttempt {
  id: string;
  paymentFailureId?: string | null;
  paymentId?: string | null;
  idempotencyKey: string;
  attemptNumber: number;
  recoveryCaseId?: string | null;
  externalRef?: string | null;
  actionType: string;
  targetRail?: string | null;
  scheduledAt?: Date | null;
  agentReasoning?: string | null;
  agentType?: string | null;
  agentConfidence?: number | null;
  guardrailPassed: boolean;
  guardrailRejectionReason?: string | null;
  executedAt?: Date | null;
  result?: AttemptResult | null;
  resultDetails?: Record<string, unknown> | null;
  nudgeMessage?: string | null;
  nudgeSent: boolean;
  channel?: string | null;
  language?: string | null;
  createdAt: Date;
}

export interface RetryLedger {
  id: number;
  customerId: string;
  totalRetries24h: number;
  totalNudges24h: number;
  lastRetryAt?: Date | null;
  lastNudgeAt?: Date | null;
  blockedUntil?: Date | null;
  consentStatus: "granted" | "opted_out";
  optedOutAt?: Date | null;
  updatedAt: Date;
}

export interface PromiseToPay {
  id: string;
  recoveryCaseId: string;
  customerId?: string | null;
  amountPromised: number;
  promisedAt: Date;
  dueAt: Date;
  channel?: Channel | string | null;
  language?: string | null;
  sourceRef?: string | null;
  status: PromiseStatus;
  resolvedAt?: Date | null;
  resolvedRef?: string | null;
  notes?: string | null;
  createdAt: Date;
}

export interface CaseEvent {
  id: number;
  recoveryCaseId: string;
  eventType: CaseEventType;
  actor: string;
  detail?: Record<string, unknown> | null;
  createdAt: Date;
}

export const PROMISE_RESOLUTION_EVENT: Record<
  PromiseResolution,
  CaseEventType
> = {
  kept: "promise_kept",
  broken: "promise_broken",
  cancelled: "promise_cancelled",
};
