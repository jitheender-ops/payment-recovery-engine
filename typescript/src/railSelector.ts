import { PAYMENT_RAILS, type PaymentRail } from "./actions.js";
import { FailureClass } from "./taxonomy.js";

export function getAvailableRails(currentMethod: string): PaymentRail[] {
  return PAYMENT_RAILS.filter((r) => r !== currentMethod);
}

export function selectAlternativeRail(
  currentMethod: string,
  failureClass = "",
): PaymentRail | null {
  const alternatives = getAvailableRails(currentMethod);
  if (alternatives.length === 0) return null;

  const preferUpi = alternatives.includes("upi");

  if (
    failureClass === FailureClass.ThreedsDropoff ||
    failureClass === FailureClass.IssuerDecline ||
    failureClass === FailureClass.CardLimitExceeded
  ) {
    return preferUpi ? "upi" : alternatives[0];
  }

  if (failureClass === FailureClass.UpiCollectTimeout) {
    return alternatives.includes("card") ? "card" : alternatives[0];
  }

  if (failureClass === FailureClass.BankDowntime) {
    if (currentMethod === "netbanking") {
      return preferUpi ? "upi" : alternatives[0];
    }
    return alternatives[0];
  }

  if (preferUpi) return "upi";
  if (alternatives.includes("card")) return "card";
  return alternatives[0];
}

export function resolveTargetRail(
  currentMethod: string,
  proposed: PaymentRail | null | undefined,
  failureClass = "",
): PaymentRail | null {
  if (proposed != null && proposed !== currentMethod) return proposed;
  return selectAlternativeRail(currentMethod, failureClass);
}
