import type { FailureContext, RetryAction } from "./actions.js";
import { parseRetryAction } from "./actions.js";
import { formatUserPrompt, SYSTEM_PROMPT } from "./prompts.js";import { fallbackAction } from "./policy.js";
import type { EngineSettings } from "./settings.js";

const TRANSIENT_RETRY_BACKOFF_MS = 1_000;
const FATAL_STATUSES = new Set([401, 402, 403, 404]);

export class LlmError extends Error {
  constructor(
    message: string,
    public readonly status?: number | undefined,
  ) {
    super(message);
    this.name = "LlmError";
  }
}

export interface LlmCompletionClient {
  complete(
    system: string,
    user: string,
    maxTokens: number,
    effort: string,
  ): Promise<string>;
}

export class PolicyAgent {
  callCount = 0;
  fallbackCount = 0;
  lastErrorStatus: number | null = null;
  lastErrorDetail: string | null = null;

  constructor(
    private readonly settings: EngineSettings,
    private readonly client: LlmCompletionClient,
    private readonly sleep: (ms: number) => Promise<void> = (ms) =>
      new Promise((r) => setTimeout(r, ms)),
  ) {}

  async decide(context: FailureContext): Promise<RetryAction> {
    this.callCount += 1;

    try {
      let rawResponse: string;
      try {
        rawResponse = await this.client.complete(
          SYSTEM_PROMPT,
          formatUserPrompt(context, this.settings.razorpayWebhookSecret),
          this.settings.llmMaxTokens,
          "medium",
        );
      } catch (firstError) {
        const status = firstError instanceof LlmError ? firstError.status : undefined;
        if (status !== undefined && FATAL_STATUSES.has(status)) throw firstError;
        await this.sleep(TRANSIENT_RETRY_BACKOFF_MS);
        rawResponse = await this.client.complete(
          SYSTEM_PROMPT,
          formatUserPrompt(context, this.settings.razorpayWebhookSecret),
          this.settings.llmMaxTokens,
          "medium",
        );
      }

      let action = this.parseResponse(rawResponse);

      if (action === null) {
        const correction = `Your previous response was not valid JSON:\n${rawResponse}\n\nPlease respond with ONLY a valid JSON object matching the RetryAction schema.`;
        rawResponse = await this.client.complete(
          SYSTEM_PROMPT,
          correction,
          this.settings.llmMaxTokens,
          "medium",
        );
        action = this.parseResponse(rawResponse);
      }

      if (action === null) {
        this.fallbackCount += 1;
        return fallbackAction(context, "LLM output could not be parsed");
      }

      return action;
    } catch (e) {
      const status = e instanceof LlmError ? e.status : undefined;
      if (status !== undefined && FATAL_STATUSES.has(status)) {
        this.lastErrorStatus = status;
        this.lastErrorDetail = `HTTP ${status}: ${String(e).slice(0, 160)}`;
      }
      this.fallbackCount += 1;
      return fallbackAction(context, `LLM error: ${String(e)}`);
    }
  }

  private parseResponse(raw: string): RetryAction | null {
    let text = raw.trim();
    if (text.startsWith("```")) {
      text = text
        .split("\n")
        .filter((ln) => !ln.trim().startsWith("```"))
        .join("\n");
    }
    try {
      const data: unknown = JSON.parse(text);
      const result = parseRetryAction(data);
      return result.ok ? result.value : null;
    } catch {
      return null;
    }
  }
}
