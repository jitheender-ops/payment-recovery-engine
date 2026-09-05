// k6 load benchmark — the engine's hot paths, per docs/SCALING.md's own
// capacity math (peak diwali-shaped bursts 300-500 webhooks/s; a few API
// replicas over Postgres carry the request path).
//
// What this file measures and why:
//   POST /risks        the ingestion money path — signed, idempotent, the
//                      surface every failure funnels through. This is the
//                      path that has to hold when the sale goes wrong for
//                      a million people at once.
//   GET  /health       the LB/uproxy liveness probe — cheap, must never wobble.
//   GET  /foundation   a representative server-rendered public page.
//
// SLOs (the "100/100" bar, encoded as thresholds — k6 exits non-zero if any
// fails, which is what makes this a benchmark rather than a load generator):
//   - zero failed requests on every path
//   - p95 ingestion latency under 250ms at 300 RPS (SCALING.md's peak band)
//   - p95 health under 50ms
//
// Run against the demo boot:  ./run.sh --demo   then
//   k6 run scripts/load_benchmark.js
// The demo's fixed local secrets let the HMAC be computed here.

import http from 'k6/http';
import crypto from 'k6/crypto';
import { check } from 'k6';

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const RISK_SECRET = __ENV.RISK_SECRET || 'demo_risk_secret_local_only';

// ── The shape of a real /risks event (ingestion/risk_router.py RiskEventIn) ──
function riskEvent(i) {
  // checkout_abandonment: one of the four chased risk types (risk_router.py
  // ChasedRiskType) — the cart chaser's intake. Chasing itself happens on
  // the scheduler's clock, off this request path, so this measures the
  // ingestion surface pure: signature, validate, dedup, write-ahead insert.
  return JSON.stringify({
    risk_type: 'checkout_abandonment',
    reference_id: `bench_order_${i}`,
    amount_paise: 100000 + (i % 50) * 100,
    customer_contact: `9${(700000000 + (i % 1000000)).toString().slice(0, 9)}`,  // 10-digit Indian mobile, ≤20 chars
    occurred_at: new Date().toISOString(),
    meta: { cart_items: ['groceries'] },
  });
}

function sign(body, secret) {
  return crypto.hmac('sha256', secret, body, 'hex');
}

export const options = {
  // constant-arrival-rate, not VU ramping: the requirement in
  // docs/SCALING.md is a diwali-shaped burst of 300-500 webhooks/SECOND.
  // Ramping VUs measures how many concurrent connections survive, which is
  // a different (and much harsher) question — 300 VUs each hammering as
  // fast as they can is thousands of RPS, past any stated capacity. This
  // executor issues exactly the arrival rate we promised to sustain and
  // lets k6 size the VUs, which is what a capacity benchmark must do.
  scenarios: {
    peak_burst: {
      executor: 'constant-arrival-rate',
      rate: __ENV.RPS ? parseInt(__ENV.RPS) : 500,   // ingest events/s — SCALING.md's peak band
      timeUnit: '1s',
      duration: __ENV.DURATION || '60s',
      preAllocatedVUs: 50,
      maxVUs: 300,
    },
  },
  thresholds: {
    http_req_failed: ['rate==0.00'],
    'http_req_duration{page:ingest}': ['p(95)<250'],
    'http_req_duration{page:health}': ['p(95)<50'],
    checks: ['rate==1.00'],
    // The arrival-rate contract itself: dropped iterations = the engine
    // could not accept events at the promised rate.
    dropped_iterations: ['count==0'],
  },
};

export default function () {
  const i = __ITER * 1000 + __VU;

  // Ingestion — the money path. Unique subject_ref per request: this is the
  // no-collision branch (the collision branch is exercised by the suite's
  // idempotency tests; a load run must not depend on prior state).
  const body = riskEvent(i);
  const res = http.post(`${BASE}/risks`, body, {
    headers: {
      'Content-Type': 'application/json',
      'X-Risk-Signature': sign(body, RISK_SECRET),
    },
    tags: { page: 'ingest' },
  });
  check(
    res,
    {
      'ingest accepted (200/202)': (r) => r.status === 200 || r.status === 202,
    },
    { page: 'ingest' }
  );

  // Liveness — what the load balancer polls between bursts.
  const h = http.get(`${BASE}/health`, { tags: { page: 'health' } });
  check(h, { 'health 200': (r) => r.status === 200 }, { page: 'health' });

  // One public page per iteration keeps the render path honest too.
  const p = http.get(`${BASE}/foundation`, { tags: { page: 'public' } });
  check(p, { 'public 200': (r) => r.status === 200 }, { page: 'public' });
}
