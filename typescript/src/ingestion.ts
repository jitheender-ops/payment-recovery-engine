import { randomUUID } from "node:crypto";
import { verifyWebhookSignature } from "./signature.js";
import * as recoveryLink from "./recoveryLink.js";
import type { EngineSettings } from "./settings.js";
import type { RecoveryStore } from "./store.js";
import type { WebhookEvent } from "./entities.js";
import { PaymentRecoveryOrchestrator } from "./orchestrator.js";
import { attributeCapture } from "./cases.js";

export interface WebhookAck {
  status: 200 | 400 | 401;
  body: string;
}

export function constructEventId(payload: Record<string, unknown>): string {
  const entity = readPaymentEntity(payload);
  const eventType = String(payload["event"] ?? "unknown");
  const paymentId = String(entity["id"] ?? "unknown");
  const createdAt = Number(payload["created_at"] ?? 0);
  return `${eventType}_${paymentId}_${createdAt}`;
}

function readPaymentEntity(payload: Record<string, unknown>): Record<string, unknown> {
  const p = payload["payload"] as Record<string, unknown> | undefined;
  const payment = p?.["payment"] as Record<string, unknown> | undefined;
  return (payment?.["entity"] as Record<string, unknown>) ?? {};
}

export async function receiveRazorpayWebhook(
  rawBody: Buffer | string,
  signature: string | null | undefined,
  deps: {
    store: RecoveryStore;
    settings: EngineSettings;
    orchestrator: PaymentRecoveryOrchestrator;
  },
  now: Date = new Date(),
): Promise<WebhookAck> {
  if (!signature || !verifyWebhookSignature(rawBody, signature, deps.settings.razorpayWebhookSecret)) {
    return { status: 401, body: "Invalid signature" };
  }

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(typeof rawBody === "string" ? rawBody : rawBody.toString("utf8"));
  } catch {
    return { status: 400, body: "Invalid JSON" };
  }

  const eventType = String(payload["event"] ?? "unknown");
  const eventId = constructEventId(payload);

  const isNew = await deps.store.recordProcessedEvent(eventId);
  if (!isNew) {
    return { status: 200, body: "Already processed" };
  }

  const event: WebhookEvent = {
    id: randomUUID(),
    razorpayEventId: eventId,
    eventType,
    payload,
    receivedAt: now,
    processed: false,
    processingError: null,
  };
  await deps.store.insertEvent(event);

  if (eventType === "payment.failed" || eventType === "payment.captured") {
    await processEventBackground(eventId, eventType, payload, deps, now);
  }

  return { status: 200, body: "OK" };
}

export async function processEventBackground(
  eventId: string,
  eventType: string,
  payload: Record<string, unknown>,
  deps: {
    store: RecoveryStore;
    settings: EngineSettings;
    orchestrator: PaymentRecoveryOrchestrator;
  },
  now: Date = new Date(),
): Promise<void> {
  if (eventType === "payment.failed") {
    const event = await deps.store.getEventByRazorpayId(eventId);
    if (event) {
      await deps.orchestrator.processPaymentFailure(event, now);
      await deps.store.markEventProcessed(eventId);
    }
    return;
  }

  if (eventType === "payment.captured") {
    const entity = readPaymentEntity(payload);
    const paymentId = entity["id"] as string | undefined;
    if (!paymentId) return;

    const notes = (entity["notes"] as Record<string, unknown> | null) ?? {};
    const linkPayload = (payload["payload"] as Record<string, unknown> | undefined)?.[
      "payment_link"
    ] as Record<string, unknown> | undefined;
    const linkEntity = (linkPayload?.["entity"] as Record<string, unknown>) ?? {};

    await attributeCapture(
      deps.store,
      deps.settings,
      {
        amount: Number(entity["amount"] ?? 0),
        recoveredRef: paymentId,
        linkId: (linkEntity["id"] as string) ?? null,
        idempotencyKey: (notes["retry_idempotency_key"] as string) ?? null,
        orderRef: (entity["order_id"] as string) ?? null,
      },
      now,
    );
  }
}

export { recoveryLink };
