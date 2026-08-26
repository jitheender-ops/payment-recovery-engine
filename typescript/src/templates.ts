function escapeEntities(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

interface TemplateVars {
  name: string | null;
  amount: string;
  nextStep: string;
}

function hi(v: TemplateVars): string {
  return v.name ? `Hi ${escapeEntities(v.name)}, ` : "Hi, ";
}

const TEMPLATES: Record<string, (v: TemplateVars) => string> = {
  insufficient_funds: (v) =>
    `${hi(v)}your ₹${v.amount} payment didn't go through due to low balance. ${escapeEntities(v.nextStep)}`,
  bank_downtime: (v) =>
    `${hi(v)}your ₹${v.amount} payment failed due to a temporary bank issue. We'll retry shortly.`,
  "3ds_dropoff": (v) =>
    `${hi(v)}your ₹${v.amount} payment needs OTP verification. ${escapeEntities(v.nextStep)}`,
  upi_collect_timeout: (v) =>
    `${hi(v)}your ₹${v.amount} UPI payment timed out. Please approve the request or try again.`,
  issuer_decline: (v) =>
    `${hi(v)}your ₹${v.amount} payment was declined by your bank. ${escapeEntities(v.nextStep)}`,
  network_error: (v) =>
    `${hi(v)}your ₹${v.amount} payment hit a temporary glitch. We're retrying automatically.`,
  card_limit_exceeded: (v) =>
    `${hi(v)}your ₹${v.amount} payment exceeded your card limit. Try a different card or UPI.`,
  payment_timeout: (v) =>
    `${hi(v)}your ₹${v.amount} payment timed out. We're retrying automatically.`,
};

const FALLBACK_TEMPLATE = (v: TemplateVars) =>
  `${hi(v)}your ₹${v.amount} payment didn't go through. ${escapeEntities(v.nextStep)}`;

export function getTemplate(failureClass: string): (v: TemplateVars) => string {
  return TEMPLATES[failureClass] ?? FALLBACK_TEMPLATE;
}

export function renderFallback(
  failureClass: string,
  amountDisplay: string,
  nextStep = "Please try again or use a different payment method.",
  customerName: string | null = null,
): string {
  return getTemplate(failureClass)({ name: customerName, amount: amountDisplay, nextStep });
}
