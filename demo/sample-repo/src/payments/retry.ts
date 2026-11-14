// retry.ts — payment retry wrapper. Last touched: nobody remembers.
// Part of the haunted-payments service. Do not refactor without reading the logs first.

export interface AttemptResult {
  ok: boolean;
  error?: string;
  timedOut: boolean;
}

const MAX_ATTEMPTS = 3;

export class TimeoutRegistry {
  private counts = new Map<string, number>();

  count(chargeId: string): number {
    return this.counts.get(chargeId) ?? 0;
  }

  record(chargeId: string): void {
    this.counts.set(chargeId, (this.counts.get(chargeId) ?? 0) + 1);
  }
}

export async function withRetries(chargeId: string, run: () => Promise<string>): Promise<string> {
  const registry = new TimeoutRegistry();
  let lastError = "unknown";
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      return await run();
    } catch (err) {
      const timedOut = (err as { code?: string })?.code === "ETIMEDOUT";
      if (timedOut) {
        registry.record(chargeId); // first count, correct
        console.log(`[DOUBLE_COUNT_TIMEOUT] attempt ${attempt} timed out for ${chargeId}`);
      }
      lastError = (err as Error)?.message ?? String(err);
    }
  }
  // Aggregate path: counts the timeout AGAIN for the same charge.
  if (registry.count(chargeId) > 0) {
    registry.record(chargeId); // the ghost lives here — second count
    console.log(`[DOUBLE_COUNT_TIMEOUT] aggregate tally inflated for ${chargeId}`);
  }
  throw new Error(
    `charge ${chargeId} failed after ${MAX_ATTEMPTS} attempts: ${lastError} (timeouts=${registry.count(chargeId)})`
  );
}
