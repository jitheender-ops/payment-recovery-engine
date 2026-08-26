export const ACTION_TYPES = [
  "retry_now",
  "retry_at",
  "switch_rail",
  "nudge_customer",
  "abandon",
] as const;

export type ActionType = (typeof ACTION_TYPES)[number];

export const PAYMENT_RAILS = ["upi", "card", "netbanking", "wallet"] as const;

import { FailureClass } from "./taxonomy.js";

export type PaymentRail = (typeof PAYMENT_RAILS)[number];

export interface RetryAction {
  action: ActionType;
  rail?: PaymentRail | null;
  retryAt?: Date | null;
  reason: string;
  confidence?: number | null;
}

export interface FailureContext {
  paymentId: string;
  orderId?: string | null;
  failureClass: string;
  errorCode: string;
  errorDescription?: string | null;
  errorSource?: string | null;
  errorReason?: string | null;
  amount: number;
  currency: string;
  method: string;
  bank?: string | null;
  cardNetwork?: string | null;
  cardType?: string | null;
  customerId?: string | null;
  customerEmail?: string | null;
  customerContact?: string | null;
  retryCount24h: number;
  nudgeCount24h: number;
  previousRetryOutcomes: string[];
  failedAt: Date;
  currentTime: Date;
  hourOfDay: number;
  dayOfWeek: number;
  isRetryable: boolean;
  originalFailureId?: string | null;
}

export type ValidationResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: string };

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function parseRetryAction(input: unknown): ValidationResult<RetryAction> {
  if (!isObject(input)) return { ok: false, error: "action must be a JSON object" };

  const action = input["action"];
  if (typeof action !== "string" || !(ACTION_TYPES as readonly string[]).includes(action)) {
    return {
      ok: false,
      error: `action must be one of ${ACTION_TYPES.join(", ")}`,
    };
  }

  const reason = input["reason"];
  if (typeof reason !== "string" || reason.length < 5 || reason.length > 500) {
    return { ok: false, error: "reason must be a string of 5-500 characters" };
  }

  const railInput = input["rail"];
  let rail: PaymentRail | null = null;
  if (railInput !== undefined && railInput !== null) {
    if (typeof railInput !== "string" || !(PAYMENT_RAILS as readonly string[]).includes(railInput)) {
      return {
        ok: false,
        error: `rail must be one of ${PAYMENT_RAILS.join(", ")} or null`,
      };
    }
    rail = railInput as PaymentRail;
  }

  const confidenceInput = input["confidence"];
  let confidence: number | null = null;
  if (confidenceInput !== undefined && confidenceInput !== null) {
    if (
      typeof confidenceInput !== "number" ||
      !Number.isFinite(confidenceInput) ||
      confidenceInput < 0 ||
      confidenceInput > 1
    ) {
      return { ok: false, error: "confidence must be a number between 0 and 1" };
    }
    confidence = confidenceInput;
  }

  const retryAtInput = input["retry_at"] ?? input["retryAt"];
  let retryAt: Date | null = null;
  if (retryAtInput !== undefined && retryAtInput !== null) {
    const d = retryAtInput instanceof Date ? retryAtInput : new Date(retryAtInput as string);
    if (Number.isNaN(d.getTime())) {
      return { ok: false, error: "retry_at must be a valid datetime" };
    }
    retryAt = d;
  }

  return {
    ok: true,
    value: { action: action as ActionType, rail, retryAt, reason, confidence },
  };
}

export function buildFailureContext(input: Record<string, unknown>): FailureContext {
  return {
    paymentId: String(input["paymentId"] ?? ""),
    orderId: (input["orderId"] as string | undefined) ?? null,
    failureClass: String(input["failureClass"] ?? FailureClass.Unknown),
    errorCode: String(input["errorCode"] ?? "UNKNOWN"),
    errorDescription: (input["errorDescription"] as string | undefined) ?? null,
    errorSource: (input["errorSource"] as string | undefined) ?? null,
    errorReason: (input["errorReason"] as string | undefined) ?? null,
    amount: Number(input["amount"] ?? 0),
    currency: String(input["currency"] ?? "INR"),
    method: String(input["method"] ?? "unknown"),
    bank: (input["bank"] as string | undefined) ?? null,
    cardNetwork: (input["cardNetwork"] as string | undefined) ?? null,
    cardType: (input["cardType"] as string | undefined) ?? null,
    customerId: (input["customerId"] as string | undefined) ?? null,
    customerEmail: (input["customerEmail"] as string | undefined) ?? null,
    customerContact: (input["customerContact"] as string | undefined) ?? null,
    retryCount24h: Number(input["retryCount24h"] ?? 0),
    nudgeCount24h: Number(input["nudgeCount24h"] ?? 0),
    previousRetryOutcomes: (input["previousRetryOutcomes"] as string[]) ?? [],
    failedAt: new Date(input["failedAt"] as string | number | Date),
    currentTime: input["currentTime"] ? new Date(input["currentTime"] as string) : new Date(),
    hourOfDay: Number(input["hourOfDay"] ?? 0),
    dayOfWeek: Number(input["dayOfWeek"] ?? 0),
    isRetryable: Boolean(input["isRetryable"] ?? true),
    originalFailureId: (input["originalFailureId"] as string | undefined) ?? null,
  };
}
