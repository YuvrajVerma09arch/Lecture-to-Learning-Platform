# Shipping checklist and rollback plan

State of the release at commit `df845f6` (20 Nov 2026). This is the evidence
used to decide whether a rollout is safe, and what to do when it is not.

## Gates passed

| Gate | Result |
|---|---|
| Python suite | `PYTHONPATH=backend .venv/bin/python -m pytest -q` — 414 passed |
| Lint | `.venv/bin/ruff check .` — all checks passed |
| Build | `cd web && npm run build` — `tsc --noEmit` and Vite build passed |
| End-to-end | `.venv/bin/python scripts/e2e_smoke.py` — 32 passed, 0 failed |
| Python dependencies | `pip-audit` — no known vulnerabilities (local editable project skipped) |
| JS dependencies | `npm audit --audit-level=high` — no high/critical; 4 low only (Vite 8 upgrade cleared the dev-only findings) |
| Isolation | `backend/tests/test_ownership.py` — 18 passed |
| Initial payload | 188.68 kB gzip JS (< 200 kB), 14.99 kB gzip CSS (< 50 kB) |
| Client metrics (synthetic) | LCP 232 ms, FCP 204 ms, CLS 0, TTFB 55 ms |
| Security | CSP/frame/object/base-uri headers, rate limits, specific CORS origins, auth gates — all asserted by the E2E smoke |
| Code review | Tutor grounding reviewed; prompt-injection boundary and worker wiring fixed in `ed71eb5` / `df845f6` |

## Accepted deviations

- Vite warns about chunks over 500 kB **uncompressed**. Gzipped sizes are
  inside budget and measured LCP is 232 ms; the LiveKit client is already
  lazy-loaded on `/chat`. Do not split further without fresh evidence.
- Web Vitals above are synthetic and unthrottled; there is no field data yet.
- No axe-core/Lighthouse accessibility run has been recorded.
- No centralised error reporting or p95 latency dashboard exists yet.

## Open before external exposure

- Set `SESSION_COOKIE_SECURE=true` and serve HTTPS; the default is the local
  `http://` demo.
- Configure production values for `FRONTEND_URL`, `ALLOWED_ORIGINS`,
  `SESSION_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and the
  provider keys. Open mode (no Google client id) is for the local demo only.
- Add error reporting and Web Vitals collection; run axe-core once.

## Rollout sequence

1. Deploy to a staging environment and re-run all gates above.
2. Deploy to production with sign-in configured; verify `/api/health` and
   `/api/ready` before sending traffic.
3. Manually exercise sign-in → upload → notes → doubt chat → flashcards.
4. Watch logs and error rate for the first hour. There are no feature flags;
   risky behaviour is controlled by the environment variables listed above,
   which can be reverted without a code deploy.

## Rollback plan

**Triggers:** error rate above 2x baseline, p95 latency more than 50% above
baseline, a spike in user-reported issues, any data-integrity problem, or a
newly discovered security vulnerability.

1. If a configuration change caused it, restore the previous environment
   values and restart the processes.
2. Otherwise redeploy the previous known-good commit (`f5b9bcf`). If the
   current batch itself must be undone in git:
   `git revert --no-edit df845f6 ed71eb5 && git push origin main`.
3. No destructive migration exists: `owner_id` was added with an additive
   `ALTER` guarded by `PRAGMA table_info`, so reverting code preserves data
   and needs no database rollback or cleanup.
4. Verify rollback with `/api/health`, `/api/ready` and one manual upload.
5. Notify users if the incident was visible to them.

Target time to rollback: under 5 minutes for code or configuration.

## First-hour verification

- `GET /api/health` returns `{"status": "ok"}`.
- `GET /api/ready` reports the database writable; provider keys need not be
  live for the probe to pass.
- Logs are flowing for session start, upload acceptance and pipeline stages.
- No new client-side error types in the browser console.
