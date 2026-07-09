"""Inspect what the Built In APPLY flow actually looks like, authenticated.

Built In hides the apply action behind a login. To see the real apply HTML:

  1. In your own browser, log in to builtinlondon.uk
  2. Install the "Cookie-Editor" extension, open it on builtinlondon.uk,
     Export -> JSON
  3. Save that as C:\\stack\\jobpipe\\app\\data\\builtin_cookies.json
  4. docker compose run --rm jobpipe-web python scripts/inspect_apply.py
     (or pass a specific job URL as an argument)

For each of the top-scoring matched Built In postings (default 5) the script
fetches the job page WITH your session, saves the raw HTML to
/app/data/apply_html/, and reports what the apply action appears to be:
an external company/ATS link, a native Built In form, an embedded iframe,
or still login-walled (cookies stale/wrong).
"""
import json
import os
import re
import sys

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, "/app")

from jobpipe.config import settings  # noqa: E402
from jobpipe.db import connect  # noqa: E402

COOKIES = "/app/data/builtin_cookies.json"
OUT_DIR = "/app/data/apply_html"
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
             "smartrecruiters.com", "oraclecloud.com", "myworkdayjobs.com",
             "icims.com", "successfactors.")


def load_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = settings.user_agent
    if not os.path.exists(COOKIES):
        print(f"NOTE: {COOKIES} not found - fetching anonymously "
              f"(expect login walls). See the docstring for cookie export steps.")
        return s
    with open(COOKIES) as f:
        for c in json.load(f):
            s.cookies.set(c["name"], c["value"],
                          domain=c.get("domain", ".builtinlondon.uk"))
    return s


def analyse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    report = {
        "login_walled": bool(re.search(r"(log ?in|sign ?up) to apply", text, re.I)),
        "external_links": [],
        "iframes": [],
        "forms": [],
    }
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = a.get_text(" ", strip=True).lower()
        if href.startswith("http") and "builtin" not in href and (
                "apply" in label or any(h in href for h in ATS_HOSTS)):
            report["external_links"].append(href)
    for fr in soup.find_all("iframe", src=True):
        report["iframes"].append(fr["src"])
    for form in soup.find_all("form"):
        inputs = [i.get("name") or i.get("id") or i.get("type", "?")
                  for i in form.find_all(["input", "textarea", "select"])]
        if inputs:
            report["forms"].append({"action": form.get("action", ""),
                                    "inputs": inputs[:15]})
    return report


def target_urls(limit: int = 5) -> list[tuple[str, str]]:
    conn = connect()
    rows = conn.execute(
        "SELECT p.id, p.title, json_extract(p.raw_json, '$.builtin_job_url') AS url "
        "FROM applications a JOIN postings p ON p.id = a.posting_id "
        "JOIN matches m ON m.posting_id = a.posting_id AND m.applicant_id = a.applicant_id "
        "WHERE p.source = 'builtin' AND json_extract(p.raw_json, '$.builtin_job_url') "
        "IS NOT NULL ORDER BY m.score DESC LIMIT ?", (limit,)).fetchall()
    return [(f"{r['id']}_{re.sub(r'[^a-z0-9]+', '-', r['title'].lower())[:40]}",
             r["url"]) for r in rows]


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    session = load_session()
    targets = ([("manual", sys.argv[1])] if len(sys.argv) > 1 else target_urls())
    if not targets:
        print("no matched Built In postings found - run the matcher first")
        return
    for slug, url in targets:
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as e:
            print(f"\n== {url}\n   FETCH FAILED: {e}")
            continue
        path = os.path.join(OUT_DIR, f"{slug}.html")
        with open(path, "w") as f:
            f.write(resp.text)
        r = analyse(resp.text)
        print(f"\n== {url}\n   saved: {path}  (HTTP {resp.status_code}, "
              f"{len(resp.text)} bytes)")
        print(f"   login-walled: {r['login_walled']}")
        for link in r["external_links"][:5]:
            print(f"   external apply link: {link}")
        for src in r["iframes"][:3]:
            print(f"   iframe: {src}")
        for form in r["forms"][:3]:
            print(f"   form action={form['action']!r} inputs={form['inputs']}")
        if not any((r["external_links"], r["iframes"], r["forms"])):
            print("   no apply mechanism visible in static HTML "
                  "(likely JS-rendered - Playwright needed)")


if __name__ == "__main__":
    main()
