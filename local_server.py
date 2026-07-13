"""Run the app locally: serves index.html + static files and routes
POST /api/search through the same handler code that runs on Vercel.

    python3.13 local_server.py          # http://localhost:3210  (PORT=… to change)

Put OPENROUTER_API_KEY in AisearchTravel2/.env (read on every request,
so you can add/rotate the key without restarting):

    OPENROUTER_API_KEY=sk-or-...
    # MODEL_IDS=model-a,model-b        # optional override, primary first
"""

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "api"))
import analytics  # noqa: E402
import search  # noqa: E402  (builds the fuzzy index — a few seconds)

PORT = int(os.environ.get("PORT", 3210))


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


class LocalHandler(search.handler, SimpleHTTPRequestHandler):
    """do_GET (static files) from SimpleHTTPRequestHandler,
    do_POST (/api/search) from the Vercel handler."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0].rstrip("/") == "/api/analytics":
            analytics.handler.do_GET(self)
        else:
            super().do_GET()

    def do_POST(self):  # noqa: N802
        load_env()
        # honor MODEL_IDS changes in .env without a restart
        search.MODELS = [
            m.strip()
            for m in os.environ.get("MODEL_IDS", "openai/gpt-oss-120b,openai/gpt-4o-mini").split(",")
            if m.strip()
        ]
        super().do_POST()


if __name__ == "__main__":
    load_env()
    key = os.environ.get("OPENROUTER_API_KEY", "")
    print(f"OPENROUTER_API_KEY: {'set (' + key[:12] + '...)' if key else 'NOT SET — AI Chat will error; Quick Search still works'}")
    print(f"serving http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), LocalHandler).serve_forever()
