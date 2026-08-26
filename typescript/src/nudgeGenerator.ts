import { renderFallback } from "./templates.js";
import type { EngineSettings } from "./settings.js";

export interface LlmTextClient {
  complete(system: string, user: string, maxTokens: number): Promise<string>;
}

const NUDGE_SYSTEM_PROMPT = `Generate a brief, empathetic payment failure notification for a customer.
Rules:
- Max 160 characters (SMS limit)
- Be clear about the issue and next step
- Tone: helpful, not pushy
- Do NOT include any links or URLs
- Output ONLY the message text, nothing else
`.replace(/\n$/, "");

export class NudgeGenerator {
  private client: LlmTextClient | null | undefined;

  constructor(
    private readonly settings: EngineSettings,
    private readonly clientFactory?: () => LlmTextClient | null,
  ) {
    this.client = undefined;
  }

  private getClient(): LlmTextClient | null {
    if (this.client !== undefined) return this.client;
    this.client = this.clientFactory ? this.clientFactory() : null;
    return this.client;
  }

  async generate(
    failureClass: string,
    amount: number,
    method: string,
    nextStep: string,
    customerName: string | null = null,
    merchantName = "the merchant",
  ): Promise<string> {
    const amountDisplay = (amount / 100).toLocaleString("en-IN", {
      minimumFractionDigits: 2,
    });

    const client = this.getClient();
    if (client !== null) {
      try {
        const userPrompt = `Payment of ₹${amountDisplay} via ${method} failed. Reason: ${failureClass.replace(/_/g, " ")}. Customer name: ${customerName ?? "unknown"}. Merchant: ${merchantName}. Suggested next step: ${nextStep}. Generate the notification message (max 160 chars).`;
        const message = await client.complete(NUDGE_SYSTEM_PROMPT, userPrompt, 100);
        const trimmed = message.trim();
        if (trimmed && trimmed.length <= 200) {
          return trimmed.slice(0, 160);
        }
      } catch {
        /* fall through to template */
      }
    }

    return renderFallback(failureClass, amountDisplay, nextStep, customerName).slice(0, 160);
  }
}
