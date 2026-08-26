import type { PaymentFailure } from "./entities.js";

export interface RazorpayClient {
  createPaymentLink(data: Record<string, unknown>): Promise<Record<string, unknown>>;
  notifyBy(linkId: string, channel: "sms" | "email"): Promise<void>;
}

export interface ExecutionResult {
  success: boolean;
  paymentLinkId?: string;
  shortUrl?: string;
  targetRail?: string | null;
  channels?: string[];
  nudgeSent?: boolean;
  nudgeMessage?: string | null;
  error?: string;
  details?: string;
}

export class RetryExecutor {
  constructor(private readonly client: RazorpayClient) {}

  async executeRetry(
    paymentFailure: PaymentFailure,
    actionType: string,
    targetRail: string | null | undefined,
    idempotencyKey: string,
    nudgeMessage?: string | null,
  ): Promise<ExecutionResult> {
    if (actionType === "abandon") {
      return { success: true, details: "No action taken" };
    }

    try {
      if (actionType === "retry_now" || actionType === "retry_at" || actionType === "switch_rail") {
        return await this.createPaymentLink(paymentFailure, targetRail, idempotencyKey);
      }
      if (actionType === "nudge_customer") {
        return await this.sendNudge(paymentFailure, idempotencyKey, nudgeMessage);
      }
      return { success: false, error: `Unknown action: ${actionType}` };
    } catch (e) {
      return { success: false, error: String(e) };
    }
  }

  private async createPaymentLink(
    failure: PaymentFailure,
    targetRail: string | null | undefined,
    idempotencyKey: string,
  ): Promise<ExecutionResult> {
    const customer: Record<string, string> = {};
    if (failure.customerEmail) customer["email"] = failure.customerEmail;
    if (failure.customerContact) customer["contact"] = failure.customerContact;

    const linkData: Record<string, unknown> = {
      amount: failure.amount,
      currency: failure.currency,
      description: `Retry payment for order ${failure.orderId ?? failure.paymentId}`,
      customer,
      notify: { sms: false, email: false },
      notes: {
        original_payment_id: failure.paymentId,
        retry_idempotency_key: idempotencyKey,
        failure_class: failure.failureClass,
        idempotency_key: idempotencyKey,
      },
    };

    const result = await this.client.createPaymentLink(linkData);

    return {
      success: true,
      paymentLinkId: result["id"] as string | undefined,
      shortUrl: result["short_url"] as string | undefined,
      targetRail: targetRail ?? null,
    };
  }

  private async sendNudge(
    failure: PaymentFailure,
    idempotencyKey: string,
    message: string | null | undefined,
  ): Promise<ExecutionResult> {
    const linkResult = await this.createPaymentLink(failure, null, idempotencyKey);
    if (!linkResult.success) return linkResult;

    const linkId = linkResult.paymentLinkId;
    const channels: string[] = [];
    if (linkId) {
      try {
        if (failure.customerContact) {
          await this.client.notifyBy(linkId, "sms");
          channels.push("sms");
        }
        if (failure.customerEmail) {
          await this.client.notifyBy(linkId, "email");
          channels.push("email");
        }
      } catch {
        /* notification failure is non-fatal */
      }
    }

    return {
      ...linkResult,
      channels,
      nudgeSent: true,
      nudgeMessage: message ?? null,
    };
  }
}
