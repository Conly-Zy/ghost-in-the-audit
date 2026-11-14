# haunted-payments — the demo target

A small payments service. Nobody on the team remembers writing it. The tests
are gone, the original author is gone, but the logs are still there — and the
logs remember everything.

Point Ghost in the Audit at this directory and let the crew work:

```bash
kirocrew spawn run "Audit demo/sample-repo for anomalies. Every finding must carry file:line and verbatim log evidence."
```

## Spoilers (for judges)

Four ghosts are planted in the code:

| Signature | Where | What |
| --- | --- | --- |
| `DOUBLE_COUNT_TIMEOUT` | `src/payments/retry.ts` | The aggregate path counts a timeout a second time, inflating the tally. |
| `SWALLOWED_CAPTURE_ERROR` | `src/payments/charge.ts` | Ledger failures are swallowed; the caller only ever sees `captured: false`. |
| `STALE_CACHE_HIT` | `src/payments/cache.ts` | `set()` overwrites values without refreshing `storedAt`, so refreshed entries expire against the wrong clock. |
| `UNHANDLED_REJECTION` | `src/api/server.ts` | The audit-write promise is fire-and-forget; its rejection goes into the void. |

Plus one anomaly that deliberately **cannot** be cited: the silent
`return { captured: false }` in `charge.ts` when the ledger responds
non-OK — no log line is ever emitted for it. The crew must report it as
`uncited` instead of inventing evidence, which is exactly the behavior the
citation gate enforces.

`logs/app.log` is the only witness. Every verifiable ghost left a
`[SIGNATURE]` line in it; the uncited one left nothing.
