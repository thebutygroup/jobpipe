"""Export ALL scanned companies to a CSV for manual review.

Every company the resolver has seen is included, with its status and what we
found. Fill manual_careers_url on ANY row to override — not just failures —
e.g. to correct a careers page that resolved to the wrong place.

Writes /app/data/unresolved_companies.csv (visible on the host at
C:\\stack\\jobpipe\\app\\data\\) and prints the CSV to stdout so it can be
copied straight from the terminal.

Workflow:
  1. python scripts/export_unresolved.py
  2. open the CSV in Excel, fill the manual_careers_url column for any
     company you can locate by hand (e.g. Admiral -> https://www.admiraljobs.co.uk/)
  3. save (Excel's "CSV UTF-8" is fine), then:
     python scripts/import_careers.py
  4. python -m jobpipe.indexscan.runner   # re-resolve: manual URLs are
     fetched directly and classified, no discovery involved
"""
import csv
import io
import sys

sys.path.insert(0, "/app")

from jobpipe.db import connect  # noqa: E402

OUT = "/app/data/companies_review.csv"
FIELDS = ["name", "idx", "status", "domain", "careers_url", "ats",
          "notes", "manual_careers_url"]


def main() -> None:
    conn = connect()
    rows = conn.execute(
        "SELECT name, idx, status, domain, careers_url, ats, notes "
        "FROM index_companies ORDER BY "
        "CASE status WHEN 'unresolved' THEN 0 WHEN 'bespoke' THEN 1 ELSE 2 END, "
        "idx, name").fetchall()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS)
    writer.writeheader()
    for r in rows:
        writer.writerow({"name": r["name"], "idx": r["idx"], "status": r["status"],
                         "domain": r["domain"] or "", "careers_url": r["careers_url"] or "",
                         "ats": r["ats"] or "", "notes": r["notes"] or "",
                         "manual_careers_url": ""})
    csv_text = buf.getvalue()
    with open(OUT, "w", newline="") as f:
        f.write(csv_text)
    print(csv_text)
    print(f"# {len(rows)} companies written to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
