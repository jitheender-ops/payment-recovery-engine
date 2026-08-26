export const FailureClass = {
  InsufficientFunds: "insufficient_funds",
  BankDowntime: "bank_downtime",
  NetworkError: "network_error",
  UpiCollectTimeout: "upi_collect_timeout",
  PaymentTimeout: "payment_timeout",
  ThreedsDropoff: "3ds_dropoff",
  IssuerDecline: "issuer_decline",
  CardLimitExceeded: "card_limit_exceeded",
  InvalidCard: "invalid_card",
  ExpiredInstrument: "expired_instrument",
  FraudBlock: "fraud_block",
  HardDecline: "hard_decline",
  CustomerCancelled: "customer_cancelled",
  Unknown: "unknown",
} as const;

export type FailureClass = (typeof FailureClass)[keyof typeof FailureClass];

export const FAILURE_CLASSES: readonly FailureClass[] = Object.values(FailureClass);

const RETRYABLE_CLASSES: ReadonlySet<FailureClass> = new Set([
  FailureClass.InsufficientFunds,
  FailureClass.BankDowntime,
  FailureClass.NetworkError,
  FailureClass.UpiCollectTimeout,
  FailureClass.PaymentTimeout,
  FailureClass.ThreedsDropoff,
  FailureClass.IssuerDecline,
  FailureClass.CardLimitExceeded,
]);

const HARD_DECLINE_CLASSES: ReadonlySet<FailureClass> = new Set([
  FailureClass.InvalidCard,
  FailureClass.ExpiredInstrument,
  FailureClass.FraudBlock,
  FailureClass.HardDecline,
  FailureClass.CustomerCancelled,
]);

const RAIL_SWITCH_CLASSES: ReadonlySet<FailureClass> = new Set([
  FailureClass.ThreedsDropoff,
  FailureClass.IssuerDecline,
  FailureClass.CardLimitExceeded,
  FailureClass.InsufficientFunds,
]);

export function isRetryable(fc: FailureClass): boolean {
  return RETRYABLE_CLASSES.has(fc);
}

export function isHardDecline(fc: FailureClass): boolean {
  return HARD_DECLINE_CLASSES.has(fc);
}

export function suggestRailSwitch(fc: FailureClass): boolean {
  return RAIL_SWITCH_CLASSES.has(fc);
}

export function toFailureClass(value: string): FailureClass | null {
  return (FAILURE_CLASSES as readonly string[]).includes(value)
    ? (value as FailureClass)
    : null;
}
