import { FailureClass } from "./taxonomy.js";

export interface ClassifierRule {
  error_code?: string;
  error_source?: string;
  error_step?: string;
  error_reason?: string;
  failure_class: FailureClass;
  retryable?: boolean;
  priority: number;
}

export const CLASSIFIER_RULES: readonly ClassifierRule[] = [
  { error_reason: "payment_risk_check_failed", failure_class: FailureClass.FraudBlock, retryable: false, priority: 100 },
  { error_reason: "card_stolen", failure_class: FailureClass.HardDecline, retryable: false, priority: 100 },
  { error_reason: "card_blocked", failure_class: FailureClass.HardDecline, retryable: false, priority: 100 },
  { error_reason: "account_closed", failure_class: FailureClass.HardDecline, retryable: false, priority: 100 },
  { error_reason: "do_not_honor", failure_class: FailureClass.HardDecline, retryable: false, priority: 95 },

  { error_reason: "payment_cancelled", failure_class: FailureClass.CustomerCancelled, retryable: false, priority: 90 },
  { error_reason: "payment_cancelled_by_customer", failure_class: FailureClass.CustomerCancelled, retryable: false, priority: 90 },

  { error_reason: "invalid_card_details", failure_class: FailureClass.InvalidCard, retryable: false, priority: 85 },
  { error_reason: "invalid_card_number", failure_class: FailureClass.InvalidCard, retryable: false, priority: 85 },
  { error_reason: "invalid_cvv", failure_class: FailureClass.InvalidCard, retryable: false, priority: 85 },
  { error_reason: "card_expired", failure_class: FailureClass.ExpiredInstrument, retryable: false, priority: 85 },
  { error_reason: "card_not_activated", failure_class: FailureClass.ExpiredInstrument, retryable: false, priority: 85 },

  { error_reason: "invalid_otp", error_step: "payment_authentication", failure_class: FailureClass.ThreedsDropoff, retryable: true, priority: 80 },
  { error_reason: "otp_timeout", error_step: "payment_authentication", failure_class: FailureClass.ThreedsDropoff, retryable: true, priority: 80 },
  { error_reason: "3ds_authentication_failed", failure_class: FailureClass.ThreedsDropoff, retryable: true, priority: 80 },
  { error_reason: "authentication_failed", error_step: "payment_authentication", failure_class: FailureClass.ThreedsDropoff, retryable: true, priority: 80 },

  { error_reason: "insufficient_funds", failure_class: FailureClass.InsufficientFunds, retryable: true, priority: 75 },
  { error_reason: "insufficient_balance", failure_class: FailureClass.InsufficientFunds, retryable: true, priority: 75 },

  { error_reason: "card_limit_exceeded", failure_class: FailureClass.CardLimitExceeded, retryable: true, priority: 75 },
  { error_reason: "daily_limit_exceeded", failure_class: FailureClass.CardLimitExceeded, retryable: true, priority: 75 },
  { error_reason: "transaction_limit_exceeded", failure_class: FailureClass.CardLimitExceeded, retryable: true, priority: 75 },

  { error_reason: "upi_collect_timeout", failure_class: FailureClass.UpiCollectTimeout, retryable: true, priority: 70 },
  { error_reason: "vpa_not_found", failure_class: FailureClass.InvalidCard, retryable: false, priority: 70 },

  { error_reason: "payment_timed_out", failure_class: FailureClass.PaymentTimeout, retryable: true, priority: 65 },
  { error_reason: "timeout", failure_class: FailureClass.PaymentTimeout, retryable: true, priority: 65 },

  { error_reason: "issuer_down", failure_class: FailureClass.BankDowntime, retryable: true, priority: 60 },
  { error_reason: "bank_technical_error", failure_class: FailureClass.BankDowntime, retryable: true, priority: 60 },
  { error_reason: "bank_unavailable", failure_class: FailureClass.BankDowntime, retryable: true, priority: 60 },
  { error_reason: "psp_error", failure_class: FailureClass.BankDowntime, retryable: true, priority: 60 },

  { error_reason: "issuer_decline", failure_class: FailureClass.IssuerDecline, retryable: true, priority: 55 },
  { error_reason: "declined_by_issuer", failure_class: FailureClass.IssuerDecline, retryable: true, priority: 55 },

  { error_code: "GATEWAY_ERROR", error_source: "gateway", failure_class: FailureClass.NetworkError, retryable: true, priority: 40 },
  { error_code: "SERVER_ERROR", error_source: "razorpay", failure_class: FailureClass.NetworkError, retryable: true, priority: 40 },
  { error_code: "SERVER_ERROR", error_source: "internal", failure_class: FailureClass.NetworkError, retryable: true, priority: 40 },

  { error_reason: "international_cards_not_supported", failure_class: FailureClass.HardDecline, retryable: false, priority: 30 },
  { error_reason: "upi_not_enabled", failure_class: FailureClass.HardDecline, retryable: false, priority: 30 },
  { error_reason: "bank_not_enabled", failure_class: FailureClass.HardDecline, retryable: false, priority: 30 },

  { error_code: "BAD_REQUEST_ERROR", error_source: "customer", failure_class: FailureClass.IssuerDecline, retryable: true, priority: 10 },
  { error_code: "BAD_REQUEST_ERROR", error_source: "business", failure_class: FailureClass.HardDecline, retryable: false, priority: 10 },
  { error_code: "GATEWAY_ERROR", failure_class: FailureClass.NetworkError, retryable: true, priority: 5 },
  { error_code: "SERVER_ERROR", failure_class: FailureClass.NetworkError, retryable: true, priority: 5 },
];
