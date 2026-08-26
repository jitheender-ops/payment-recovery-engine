export interface EngineSettings {
  maxRetriesPerPayment: number;
  maxRetriesPerCustomer24h: number;
  amountCeilingPaise: number;
  consentWindowHours: number;
  maxNudgesPerCustomer24h: number;
  rateLimitWindowHours: number;
  retryBlackoutStartHour: number;
  retryBlackoutEndHour: number;
  escalationBackoffHours: number;
  schedulerEnabled: boolean;
  schedulerIntervalSeconds: number;
  schedulerBatchSize: number;
  eventReconcileAfterSeconds: number;
  attemptStaleAfterSeconds: number;
  razorpayTimeoutSeconds: number;
  razorpayWebhookSecret: string;
  llmTimeoutSeconds: number;
  llmProvider: "anthropic" | "openai";
  llmModel: string;
  llmMaxTokens: number;
  recoveryLinkSecret: string;
  publicBaseUrl: string;
}

export const DEFAULT_SETTINGS: EngineSettings = {
  maxRetriesPerPayment: 3,
  maxRetriesPerCustomer24h: 5,
  amountCeilingPaise: 5_000_000,
  consentWindowHours: 72,
  maxNudgesPerCustomer24h: 2,
  rateLimitWindowHours: 24,
  retryBlackoutStartHour: 23,
  retryBlackoutEndHour: 7,
  escalationBackoffHours: 24,
  schedulerEnabled: true,
  schedulerIntervalSeconds: 60,
  schedulerBatchSize: 50,
  eventReconcileAfterSeconds: 300,
  attemptStaleAfterSeconds: 900,
  razorpayTimeoutSeconds: 10.0,
  razorpayWebhookSecret: "test-webhook-secret",
  llmTimeoutSeconds: 30,
  llmProvider: "openai",
  llmModel: "gpt-4o-mini",
  llmMaxTokens: 300,
  recoveryLinkSecret: "",
  publicBaseUrl: "",
};
