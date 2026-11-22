# Deployment guide (Vercel + persistent backend + LiveKit Cloud)

This is the recommended free/low-cost topology for a class demo. It keeps
your own model keys and avoids running the LiveKit agent worker on Render.

```
Browser (Vercel) ──HTTPS──> FastAPI (persistent host) ──HTTPS──> LiveKit Cloud
                                   │                              ├─ media SFU
                                   └─ SQLite on a real disk        └─ hosted agent (our Dockerfile)
```

## Why the agent goes to LiveKit Cloud

The current LiveKit **Build** plan is $0/mo and includes **1 agent
deployment, 1,000 agent session minutes/month, 5 concurrent agent sessions,
5,000 WebRTC minutes and 50 GB egress**. Hard caps mean no surprise bill.
Your Gnani / Gemini / Groq keys still work — LiveKit Inference is optional
and this project does not use it.

Deploy with `lk cloud auth`, then `lk agent create` (the root `Dockerfile`
builds the worker; the dispatch name is `lecture-tutor`), set secrets, then
`lk agent deploy`. Watch `lk agent status` and `lk agent logs`. Never set
`LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET` in the image — Cloud
injects them. See [LiveKit pricing](https://livekit.com/pricing) and the
[agent deployment quickstart](https://docs.livekit.io/deploy/agents/quickstart.md).

## Where to run the backend

Render's free web service is **ephemeral**: SQLite's `data/app.sqlite3` is
lost on restart/redeploy. Use one of:

- **GCP e2-micro Always Free** (30 GB Standard disk, us-central1 /
  us-east1 / us-west1) with Caddy for TLS — roughly $0/mo apart from a
  small public-IP charge; your $300 trial credit covers it. This keeps the
  SQLite file on a real disk and the agent's internal API reachable.
- A Render **paid** web service with a persistent disk, or a free service
  backed by an external Postgres (requires porting `db.py`, not recommended
  for a demo).

Keep a systemd service (or equivalent) for:

```
PYTHONPATH=backend .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Backend environment

```
GOOGLE_CLIENT_ID=...               # register the Vercel callback URL
GOOGLE_CLIENT_SECRET=...
FRONTEND_URL=https://your-app.vercel.app
ALLOWED_ORIGINS=https://your-app.vercel.app
SESSION_COOKIE_SAMESITE=none       # cross-site: Vercel -> backend
SESSION_SECRET=...                 # or let the app persist one in DATA_DIR
GNANI_API_KEY=...
GOOGLE_API_KEY=...
GROQ_API_KEY=...
LIVEKIT_URL=...                    # for minting room tokens
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
AGENT_API_TOKEN=<random-32-byte-token>
MAX_LECTURES_PER_USER=25
ADMIN_EMAILS=you@example.com
OPEN_MODE unset                    # never expose an open deployment
```

`Secure` is derived automatically from the HTTPS `FRONTEND_URL`; `SameSite`
must be `none` because Vercel and the API are different sites. See
`docs/SECURITY.md` and `docs/DATA_POLICY.md` for the rest of the checklist.

## Frontend

On Vercel, set `VITE_API_BASE=https://your-backend-host` and build `web/`
(`npm run build`). Register both callback URLs with Google:

```
https://your-backend-host/api/auth/google/callback
http://localhost:8000/api/auth/google/callback   # local development
```

## Hosted agent

1. `lk cloud auth`; select the project.
2. From the repo root: `lk agent create`. It uses the root `Dockerfile`
   (Python 3.12, non-root, `CMD ["python", "-m", "agent.worker"]`).
3. Set secrets: `GNANI_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`,
   `AGENT_API_URL=https://your-backend-host`, `AGENT_API_TOKEN=<same token>`.
4. `lk agent deploy`, then check `lk agent status` and `lk agent logs`.

The worker now fetches lecture orientation through
`GET /api/internal/lectures/{id}/context` with the bearer token; no database
file is needed inside the agent container.

## GCP credits: should you self-host LiveKit?

**Recommendation: not for this demo.** Current facts (22 Nov 2026):

- The $300 trial credit lasts **90 days**, then the billing account
  auto-closes (you are not charged unless you manually upgrade). Unused
  credit expires. Always Free resources require an active billing account.
- A sensible single-box LiveKit stack is an **e2-standard-2** (2 vCPU /
  8 GB): about **$49/mo** plus a ~$3.65 public IP after credits — ~$53/mo.
  It would use only ~$159 of your $300 before expiry.
- Self-hosting means owning TURN/TLS, ports 80/443/3478/7880/7881 and
  50000–60000/UDP, Redis if you add a second node, egress service, metrics
  and manual upgrades. LiveKit Cloud does all of that, plus global edge
  routing and observability.
- A full class demo (20 students × 30 min) uses ~600 agent-minutes and
  ~600 WebRTC minutes — comfortably inside the free Build caps.

Reconsider self-hosting only for sustained >5k–15k agent minutes/month,
data-residency/private-link needs, or if operating LiveKit is itself the
learning goal. In that case prototype locally with `livekit/generate`
before spending the credits.

Sources: [GCP Free Program](https://docs.cloud.google.com/free/docs/free-cloud-features) ·
[GCP E2 pricing](https://cloud.google.com/products/compute/pricing/general-purpose#e2-machine-types) ·
[LiveKit self-hosting](https://docs.livekit.io/transport/self-hosting.md) ·
[LiveKit quotas](https://docs.livekit.io/deploy/admin/quotas-and-limits.md)
