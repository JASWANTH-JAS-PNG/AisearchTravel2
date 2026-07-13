"""Force-(re)build search_index.pkl from accounts.db.

The API builds and saves the pickle automatically on first cold start when it's
missing or stale (keyed on INDEX_VERSION + DB mtime); run this after regenerating
accounts.db so the first request doesn't pay the build.

    python3.13 build_search_index.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))

for p in [Path(__file__).resolve().parent / "search_index.pkl"]:
    p.unlink(missing_ok=True)

t0 = time.time()
import search  # noqa: E402  (import builds + saves the index)

out = search.INDEX_PICKLE_CANDIDATES[0]
print(f"built in {time.time() - t0:.1f}s -> {out} "
      f"({out.stat().st_size / 1e6:.0f} MB, {len(search._FREQ):,} vocab tokens, "
      f"{len(search._DELETES):,} delete variants)")
