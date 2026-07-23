"""Verify aggregator API keys live and record real responses as fixtures.

Run this ON A MACHINE WITH NORMAL INTERNET (Mac or the home server) after
putting the keys in app/.env:

    cd app && python scripts/record_fixtures.py

For each configured aggregator it makes ONE small live call, prints what came
back (so you instantly know whether the key works), and writes the raw
response to tests/fixtures/<source>_search_live.json. The test suite picks
those files up automatically (tests skip while they don't exist), so commit
them: from then on CI validates normalization against real payload shapes,
not just the hand-written toys.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobpipe.sources import registry  # noqa: E402
from jobpipe.sources.base import SearchSpec  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"
KEEP = 10  # postings per recorded fixture — enough shape, small diff


def main() -> None:
    spec = SearchSpec(source="record", name="record-fixtures",
                      keywords="data engineer", location="London")
    any_configured = False
    for source, adapter in registry.aggregators().items():
        if not adapter.is_configured():
            print(f"[{source}] unconfigured — {adapter.unconfigured_reason()}")
            continue
        any_configured = True
        print(f"[{source}] key present; calling live API…")
        try:
            raws = adapter.fetch(spec)
        except Exception as e:
            print(f"[{source}] FAILED: {e}")
            print(f"[{source}] -> key wrong, quota hit, or network blocked. "
                  f"Fix and re-run.")
            continue
        print(f"[{source}] OK — {len(raws)} results")
        for raw in raws[:3]:
            dto = adapter.normalize(raw, spec)
            print(f"    {dto.title!r} @ {dto.company_name!r} ({dto.location}) "
                  f"salary={dto.raw['salary_normalised']}")
        out = FIXTURES / f"{source}_search_live.json"
        out.write_text(json.dumps({"results": raws[:KEEP]}, indent=1))
        print(f"[{source}] recorded {min(len(raws), KEEP)} postings -> {out}")
    if not any_configured:
        print("\nNo aggregator keys set. Add to app/.env:\n"
              "  REED_API_KEY=…\n  ADZUNA_APP_ID=…\n  ADZUNA_APP_KEY=…")
        sys.exit(1)
    print("\nDone. Commit the *_live.json fixtures so CI validates against "
          "real payload shapes.")


if __name__ == "__main__":
    main()
