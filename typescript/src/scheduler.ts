import type { RetryAction } from "./actions.js";
import type { EngineSettings } from "./settings.js";
import type { RecoveryStore } from "./store.js";
import type { RetryAttempt } from "./entities.js";
import { PaymentRecoveryOrchestrator } from "./orchestrator.js";
import { expirePromises, logEvent } from "./cases.js";

export interface SchedulerDeps {
  store: RecoveryStore;
  settings: EngineSettings;
  orchestrator: PaymentRecoveryOrchestrator;
}

export async function fireDueRetries(
  deps: SchedulerDeps,
  now: Date = new Date(),
): Promise<number> {
  const due = await deps.store.listDueScheduledAttempts(now, deps.settings.schedulerBatchSize);

  let fired = 0;
  for (const attempt of due) {
    const claim = await deps.store.claimScheduledAttempt(attempt.id);
    if (!claim.claimed) continue;
    if (await fireOne(deps, claim.attempt, now)) fired++;
  }
  return fired;
}

async function fireOne(
  deps: SchedulerDeps,
  attempt: RetryAttempt,
  now: Date,
): Promise<boolean> {
  const { store, settings, orchestrator } = deps;

  if (attempt.paymentFailureId == null) {
    await mark(store, attempt, "skipped", "no payment_failure to retry");
    return false;
  }

  const failure = await store.getFailure(attempt.paymentFailureId);
  if (failure === null) {
    await mark(store, attempt, "skipped", "payment_failure row is gone");
    return false;
  }

  const kase = attempt.recoveryCaseId ? await store.getCase(attempt.recoveryCaseId) : null;

  if (kase !== null) {
    const ledger = kase.customerId ? await store.getLedger(kase.customerId) : null;
    let stop: string | null = null;
    if (kase.state !== "open") {
      stop = `case is ${kase.state}: ${kase.closeReason ?? "no reason recorded"}`;
    } else if (ledger !== null && ledger.consentStatus === "opted_out") {
      stop = "customer opted out of contact";
    }
    if (stop !== null) {
      await mark(store, attempt, "cancelled", stop);
      store.saveCase(kase);
      await logEvent(store, kase, "stopped", "scheduler", { reason: stop, at: "fire_time" });
      return false;
    }
  }

  const context = await orchestrator.buildFailureContext(failure, now);
  const action: RetryAction = {
    action: attempt.targetRail ? "switch_rail" : "retry_now",
    rail: (attempt.targetRail ?? undefined) as RetryAction["rail"],
    reason: attempt.agentReasoning ?? "scheduled retry",
    confidence: attempt.agentConfidence,
  };

  const guardrail = orchestrator["guardrail"].validate(
    action,
    context,
    attempt.idempotencyKey,
    attempt.attemptNumber - 1,
    now,
  );
  if (!guardrail.passed) {
    const reason = guardrail.rejectionReasons.join("; ");
    await mark(store, attempt, "rejected", reason);
    if (kase !== null) {
      store.saveCase(kase);
      await logEvent(store, kase, "stopped", "scheduler", { reason, at: "fire_time" });
    }
    return false;
  }

  if (kase === null) {
    await mark(store, attempt, "skipped", "no recovery case");
    return false;
  }

  await orchestrator.executeAndRecord(
    attempt,
    kase,
    failure,
    action,
    attempt.idempotencyKey,
    "scheduler",
    now,
  );
  store.saveCase(kase);
  return true;
}

async function mark(
  store: RecoveryStore,
  attempt: RetryAttempt,
  result: RetryAttempt["result"],
  reason: string,
): Promise<void> {
  attempt.result = result;
  attempt.executedAt = new Date();
  attempt.resultDetails = { scheduler: reason };
  store.saveAttempt(attempt);
}

export async function reconcileEvents(
  deps: SchedulerDeps,
  now: Date = new Date(),
  processEvent: (event: Awaited<ReturnType<RecoveryStore["getEventByRazorpayId"]>>) => Promise<void>,
): Promise<number> {
  const cutoff = new Date(now.getTime() - deps.settings.eventReconcileAfterSeconds * 1000);
  const stale = await deps.store.listStaleUnprocessedEvents(cutoff, deps.settings.schedulerBatchSize);

  let recovered = 0;
  for (const event of stale) {
    const claim = await deps.store.claimEvent(event.id);
    if (!claim.claimed) continue;
    try {
      await processEvent(event);
      recovered++;
    } catch {
      await deps.store.markEventError(event.razorpayEventId, "Reconciliation failed");
    }
  }
  return recovered;
}

export async function reconcileStaleAttempts(
  deps: SchedulerDeps,
  now: Date = new Date(),
): Promise<number> {
  const cutoff = new Date(now.getTime() - deps.settings.attemptStaleAfterSeconds * 1000);
  const stale = await deps.store.listStalePendingAttempts(cutoff, deps.settings.schedulerBatchSize);

  let resolved = 0;
  for (const attempt of stale) {
    const claim = await deps.store.claimStalePendingAttempt(attempt.id, {
      result: "failed",
      executedAt: now,
      resultDetails: {
        scheduler: `stale-pending: no outcome after ${deps.settings.attemptStaleAfterSeconds}s — marked failed, outcome unknown (fail-closed)`,
      },
    });
    if (!claim.claimed) continue;

    if (attempt.recoveryCaseId) {
      const kase = await deps.store.getCase(attempt.recoveryCaseId);
      if (kase !== null) {
        await logEvent(deps.store, kase, "reconciled", "scheduler", {
          attempt_id: attempt.id,
          idempotency_key: attempt.idempotencyKey,
          reason: "stale pending — outcome unknown",
        });
      }
    }
    resolved++;
  }
  return resolved;
}

export async function tick(deps: SchedulerDeps, now: Date = new Date()): Promise<Record<string, number>> {
  const counts = {
    retries_fired: await fireDueRetries(deps, now),
    attempts_reconciled: await reconcileStaleAttempts(deps, now),
    promises_expired: await expirePromises(deps.store, deps.settings, now),
  };
  return counts;
}

export function startScheduler(
  deps: SchedulerDeps,
  onTick?: (counts: Record<string, number>) => void,
): { stop: () => Promise<void> } {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;

  const loop = async (): Promise<void> => {
    while (!stopped) {
      try {
        const counts = await tick(deps);
        if (onTick && Object.values(counts).some((v) => v > 0)) onTick(counts);
      } catch {
        /* one bad tick must not end the loop */
      }
      await new Promise<void>((resolve) => {
        timer = setTimeout(resolve, deps.settings.schedulerIntervalSeconds * 1000);
      });
    }
  };

  void loop();
  return {
    stop: async () => {
      stopped = true;
      if (timer !== null) clearTimeout(timer);
    },
  };
}

export { reconcileEvents as reconcileEventsSweep };
