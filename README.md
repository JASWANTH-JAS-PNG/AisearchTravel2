# AccountSearch AI — 500k

Natural-language chat search over a 500,000-account travel-booking CRM,
deployed on Vercel. Based on the Pro variant, scaled from 10k to 500k records:
the original 10,000 real CRM rows plus 490,000 synthetic accounts generated
from the same name vocabulary and patterns — now extended with 24 months
(2024-07 .. 2026-06) of per-account monthly booking activity (bookings,
revenue, profit, last reservation date) so analysts can ask booking/revenue/
recency questions, not just name searches.

(The free-tier 10k variant lives in `AiSearchTravel`; the paid-tier 10k
variant lives in `AiSearchTravel-Pro`.)

## Cost

Default models: `openai/gpt-oss-120b` (primary), `openai/gpt-4o-mini` (fallback).

| | |
|---|---|
| Typical query (both AI steps) | ~$0.00025 |
| $10 OpenRouter credit | ≈ 40,000 queries |
| 100 queries/day | ≈ $0.75/month |

## Deploy (Vercel)

1. Push this repo to GitHub and import it at https://vercel.com/new
   (no Root Directory change needed — the app is at the repo root).
2. Environment variables:
   - `OPENROUTER_API_KEY` — an OpenRouter key from an account **with credit**
     (https://openrouter.ai/credits)
   - `MODEL_IDS` — *(optional)* comma-separated model override, primary first,
     e.g. `google/gemini-2.5-flash-lite,openai/gpt-4o-mini`
3. Deploy. Done.

## Switching from free to paid (when credit is added)

The current deployment runs on free models via the `MODEL_IDS` env override
(slow, ~30s/query, shared 50-requests/day cap) because no OpenRouter credit
has been added yet. To unlock the fast paid tier:

1. Add credit (min $10) at https://openrouter.ai/credits on the account that
   owns the `OPENROUTER_API_KEY`.
2. In Vercel → this project → **Settings → Environment Variables** →
   **delete `MODEL_IDS`**.
3. **Deployments** tab → latest deployment → ⋯ → **Redeploy**.

That's it — the function falls back to the paid defaults
(`openai/gpt-oss-120b`, `openai/gpt-4o-mini`), responses drop to ~2–5s, and
the daily cap disappears. Verify with one query before handing over.

## Architecture

- `index.html` — static Claude-style chat UI. Conversation history is kept in
  the browser and sent with each request; the API key never reaches the client.
- `api/search.py` — one Python serverless function (stdlib only). Two-step
  pipeline: user question → model plans a whitelist-validated, read-only
  `SELECT` (typo correction, synonym/concept expansion, sparse-table rules for
  the monthly data) → executed read-only against `accounts.db` → matching rows
  → model writes a conversational answer citing account ids. One relaxed retry
  on empty/failed SQL; primary/fallback model rotation with backoff on provider
  errors. Plain name searches short-circuit through an in-memory fuzzy index
  (no AI call); booking/revenue/recency questions always take the AI path.
- `vercel.json` — function `maxDuration` config.

## Dataset (`accounts.db`)

Built by `generate_synthetic_db.py` (deterministic, seed 20260712, ~36s):

- `accounts(id, account_id, account_name, tier)` — 500k rows. `tier` is
  synthetic ground truth (never/occasional/good/best/elite/churned) kept for
  verification; it is deliberately NOT exposed to the AI planner, which must
  derive account quality from bookings.
- `monthly_activity(account_id, month, bookings, revenue, profit,
  last_reservation_date)` — ~7.5M rows, 2024-07 .. 2026-06. **Sparse**: a
  month with zero bookings has no row. Per trip ≈ $1,000 revenue / $200 profit
  (~10% cheap $100/$10 trips). ~20 trips/mo = good, ~30 = best, ~40 = elite.

`accounts.db` (~650 MB) and `rawdata.csv` (the reference export format) are
gitignored — regenerate locally with `python3 generate_synthetic_db.py`.
The Quick Search tab uses the bundled CSV client-side.

## Hosted DB (Turso) — how production works

The DB exceeds Vercel's 250 MB bundle limit, so the deployed functions read a
hosted copy on Turso (SQLite-compatible; database `accounts-search`). Both
endpoints prefer a **local `accounts.db` when present** (dev) and fall back to
Turso via `api/_turso.py` — a stdlib-only Hrana-over-HTTP client (no pip deps).
The fuzzy search index builds from the bundled CSV when the DB is remote, so
cold starts never stream 500k rows.

Vercel env vars needed (Settings → Environment Variables):

- `TURSO_DATABASE_URL` — `libsql://accounts-search-<org>.turso.io`
- `TURSO_AUTH_TOKEN` — a **read-only** database token
  (`turso db tokens create accounts-search --read-only`)
- `OPENROUTER_API_KEY` (+ optional `MODEL_IDS`) as before

Deployed analytics does NOT hit Turso: the read-only token forbids the TEMP
TABLE the live compute uses, so the four UI preset windows are precomputed
into `analytics_cache.json` (committed, 85 KB) by `build_analytics_cache.py`
and served from the bundle. Only the AI chat's generated SQL runs on Turso.

To refresh the hosted data after regenerating locally (the DB must be in WAL
mode — the generator sets this; token is invalidated by destroy, so mint a new
one and update the env vars):

    turso db destroy accounts-search --yes
    turso db create accounts-search --from-file accounts.db
    turso db tokens create accounts-search --read-only
    python3.13 build_analytics_cache.py

## Safety

- SELECT-only SQL validation (single statement, banned keywords, forced LIMIT)
- The model only ever sees up to 25 rows per answer
- Answers are grounded in the returned rows; the table shows ground truth
