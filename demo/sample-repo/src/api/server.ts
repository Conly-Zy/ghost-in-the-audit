// server.ts — the front door. Promises are fire-and-forget here.

import express from "express";
import { capture } from "../payments/charge";
import { TtlCache } from "../payments/cache";

const app = express();
app.use(express.json());

const resultCache = new TtlCache<string>(60_000);

async function recordAudit(chargeId: string): Promise<void> {
  try {
    await fetch("http://audit.internal:9090/entries", {
      method: "POST",
      body: JSON.stringify({ chargeId, at: new Date().toISOString() }),
    });
  } catch (err) {
    console.log(
      `[UNHANDLED_REJECTION] audit write failed for ${chargeId}: ${(err as Error).message}`
    );
    throw err; // rethrown into the void — nobody is awaiting this promise
  }
}

app.post("/charges/:id/capture", async (req, res) => {
  const chargeId = req.params.id;
  const amount = Number(req.body?.amountCents);
  if (!Number.isFinite(amount) || amount <= 0) {
    return res.status(400).json({ error: "amountCents must be positive" });
  }

  const cached = resultCache.get(chargeId);
  if (cached !== undefined) {
    return res.json({ chargeId, ...JSON.parse(cached), cached: true });
  }

  // Fire-and-forget audit write. If it rejects, nothing catches it.
  recordAudit(chargeId);

  const result = await capture(chargeId, amount);
  resultCache.set(chargeId, JSON.stringify(result));
  return res.json({ chargeId, ...result });
});

app.listen(8080);
console.log("[BOOT] haunted-payments listening on :8080");
