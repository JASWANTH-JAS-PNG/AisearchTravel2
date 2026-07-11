# AccountSearch AI — 500k

Natural-language chat search over a 500,000-row accounts dataset, deployed on
Vercel. Based on the Pro variant, scaled from 10k to 500k records: the
original 10,000 real CRM rows plus 490,000 synthetic accounts generated from
the same name vocabulary and patterns.

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
  `SELECT` (typo correction, synonym/concept expansion) → executed against
  in-memory SQLite loaded from the bundled CSV → matching rows → model writes
  a conversational answer citing account ids. One relaxed retry on
  empty/failed SQL; primary/fallback model rotation with backoff on provider
  errors.
- `vercel.json` — function `maxDuration` config.

## Safety

- SELECT-only SQL validation (single statement, banned keywords, forced LIMIT)
- The model only ever sees up to 25 rows per answer
- Answers are grounded in the returned rows; the table shows ground truth
