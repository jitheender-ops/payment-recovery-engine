import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";

const SEP = ".";

function b64(raw: Buffer): string {
  return raw.toString("base64url");
}

function unb64(text: string): Buffer {
  return Buffer.from(text, "base64url");
}

function sign(payload: string, secret: string): string {
  return b64(createHmac("sha256", secret).update(payload, "utf8").digest());
}

export function caseIdToHex(caseId: string): string {
  return caseId.replace(/-/g, "");
}

export function isValidUuidHex(hex: string): boolean {
  return /^[0-9a-f]{32}$/.test(hex);
}

export function uuidFromHex(hex: string): string {
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function newCaseId(): string {
  return randomUUID();
}

export function mint(
  caseId: string,
  secret: string,
  ttlHours: number,
  now: Date = new Date(),
): string | null {
  if (!secret) return null;
  const expiry = Math.floor(now.getTime() / 1000) + ttlHours * 3600;
  const payload = `${caseIdToHex(caseId)}${SEP}${expiry}`;
  return `${b64(Buffer.from(payload, "ascii"))}${SEP}${sign(payload, secret)}`;
}

export function verify(
  token: string,
  secret: string,
  now: Date = new Date(),
): string | null {
  if (!secret || !token || token.split(SEP).length - 1 !== 1) return null;

  const sepIndex = token.indexOf(SEP);
  const encoded = token.slice(0, sepIndex);
  const signature = token.slice(sepIndex + 1);

  let payload: string;
  try {
    payload = unb64(encoded).toString("ascii");
  } catch {
    return null;
  }

  const expected = Buffer.from(sign(payload, secret), "ascii");
  const provided = Buffer.from(signature, "utf8");
  if (expected.length !== provided.length || !timingSafeEqual(expected, provided)) {
    return null;
  }

  const parts = payload.split(SEP);
  if (parts.length !== 2) return null;
  const [caseHex, expiry] = parts;
  if (!isValidUuidHex(caseHex)) return null;
  if (!/^\d+$/.test(expiry) || Number.parseInt(expiry, 10) < Math.floor(now.getTime() / 1000)) {
    return null;
  }
  return uuidFromHex(caseHex);
}

export function urlFor(
  caseId: string,
  secret: string,
  publicBaseUrl: string,
  ttlHours: number,
  now: Date = new Date(),
): string | null {
  const token = mint(caseId, secret, ttlHours, now);
  if (token === null) return null;
  const base = publicBaseUrl.replace(/\/+$/, "");
  if (!base) return null;
  return `${base}/recover/${token}`;
}
