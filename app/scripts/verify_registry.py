"""Hit every registered ATS board once and report dead/mistyped tokens.

Run:  docker compose run jobpipe python scripts/verify_registry.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

import yaml  # noqa: E402

from jobpipe.config import settings  # noqa: E402
from jobpipe.pollers import ashby, greenhouse, lever, workable  # noqa: E402

MODULES = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable}


def main() -> int:
    with open(settings.companies_path, "r", encoding="utf-8") as fh:
        companies = (yaml.safe_load(fh) or {}).get("companies", [])
    dead = 0
    for c in companies:
        ats, token, name = c.get("ats"), c.get("board_token"), c.get("name")
        if ats not in MODULES:
            print(f"SKIP    {name}: ats={ats}")
            continue
        try:
            jobs = MODULES[ats].fetch(name, token)
            print(f"OK      {name}: {len(jobs)} open postings ({ats}/{token})")
        except Exception as e:
            dead += 1
            print(f"DEAD    {name}: {ats}/{token} -> {type(e).__name__}: {e}")
    print(f"\n{dead} dead token(s)" if dead else "\nall tokens verified")
    return 1 if dead else 0


if __name__ == "__main__":
    raise SystemExit(main())
