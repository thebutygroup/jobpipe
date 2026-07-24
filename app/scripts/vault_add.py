"""Add a document to an applicant's asset vault (owner-side, Phase 1).

Examples (from C:\\stack, file first copied into the container or on a
mounted path):
  docker compose cp resume.pdf jobpipe-web:/tmp/resume.pdf
  docker compose exec jobpipe-web python scripts/vault_add.py \\
      --user joebuty /tmp/resume.pdf                     # default variant
  docker compose exec jobpipe-web python scripts/vault_add.py \\
      --user joebuty --variant mlops /tmp/resume-mlops.pdf

Variants are matched against job titles at apply time: a resume with variant
'mlops' is picked for titles containing 'mlops'; otherwise 'default' is used.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobpipe.apply.vault import AssetVault, VaultError  # noqa: E402
from jobpipe.db import connect  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--user", required=True, help="user_ref of the applicant")
    ap.add_argument("--variant", default="default")
    ap.add_argument("--kind", default="resume")
    args = ap.parse_args()
    conn = connect()
    try:
        row = conn.execute("SELECT id FROM applicants WHERE user_ref = ?",
                           (args.user,)).fetchone()
        if not row:
            raise SystemExit(f"no applicant with user_ref {args.user!r}")
        vault = AssetVault(conn, row["id"])
        try:
            result = vault.add(args.file, kind=args.kind, variant_name=args.variant)
        except VaultError as e:
            raise SystemExit(f"refused: {e}") from None
        print(f"stored: {result}")
        print("vault now contains:")
        for a in vault.list():
            print(f"  {a['kind']}/{a['variant_name']}: {a['original_name']} "
                  f"({a['size']} bytes, {a['uploaded_at']})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
