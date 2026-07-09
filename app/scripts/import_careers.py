"""Import the reviewed unresolved-companies CSV.

Rows with manual_careers_url filled are re-queued: careers_url is set, status
returns to 'new', and on the next indexscan run the resolver fetches that
exact URL and classifies it (ATS -> registry & daily polling; bespoke ->
weekly crawler). Rows left blank are ignored.

Reads /app/data/unresolved_companies.csv by default; pass a path to override.
Tolerates Excel's UTF-8 BOM.
"""
import csv
import sys
from urllib.parse import urlsplit

sys.path.insert(0, "/app")

from jobpipe.db import connect, tx  # noqa: E402

DEFAULT = "/app/data/unresolved_companies.csv"


def main(path: str = DEFAULT) -> None:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    conn = connect()
    updated = skipped = 0
    with tx(conn):
        for row in rows:
            url = (row.get("manual_careers_url") or "").strip()
            name = (row.get("name") or "").strip()
            if not url or not name:
                skipped += 1
                continue
            if not url.startswith("http"):
                url = "https://" + url
            domain = urlsplit(url).netloc.removeprefix("www.")
            cur = conn.execute(
                "UPDATE index_companies SET careers_url = ?, domain = ?, "
                "status = 'new', notes = 'manual careers URL' WHERE name = ?",
                (url, domain, name))
            updated += cur.rowcount
    print(f"re-queued {updated} companies with manual careers URLs "
          f"({skipped} rows left blank/skipped)")
    print("now run: python -m jobpipe.indexscan.runner")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
