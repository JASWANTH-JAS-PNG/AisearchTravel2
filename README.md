# AccountSearch AI — Pro

Natural-language chat search over a 10,000-row accounts dataset, deployed on
Vercel. This is the **paid-model tier**: responses in ~2–5 seconds, no daily
request cap, pay-per-token via OpenRouter.

(The free-tier variant lives in a separate repo, `AiSearchTravel` — same app,
free models, ~20–45s responses, ~25 searches/day.)

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
