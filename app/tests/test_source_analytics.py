"""Source analytics over a small, hand-checkable provenance graph.

Graph (5 jobs, 3 sources):
  job1: greenhouse + adzuna + reed   (everyone saw it; greenhouse first)
  job2: adzuna + reed                (aggregator-only overlap; adzuna first)
  job3: adzuna                       (adzuna unique)
  job4: reed                         (reed unique)
  job5: greenhouse                   (greenhouse unique)
Expected:
  adzuna:     3 jobs, 1 unique (33%), 2 shared
  reed:       3 jobs, 1 unique (33%)
  greenhouse: 2 jobs, 1 unique (50%)
  overlap[adzuna][reed] = 2/3 = 67 ; overlap[greenhouse][adzuna] = 1/2 = 50
"""

from jobpipe import source_analytics


def _sight(conn, pid, source, ts, ext=None):
    conn.execute(
        "INSERT INTO source_postings (posting_id, source, external_id, url,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
        (pid, source, ext or f"{source}-{pid}", f"https://{source}/{pid}", ts, ts))


def _posting(conn, pid, title, score=None):
    conn.execute("INSERT INTO companies (id, name, ats) VALUES (?, ?, 'custom')",
                 (pid, f"Co{pid}"))
    conn.execute(
        "INSERT INTO postings (id, company_id, source, title, identity_key,"
        " first_seen_at, last_seen_at) VALUES (?,?,'ats',?,?,datetime('now'),"
        " datetime('now'))", (pid, pid, title, f"k{pid}"))
    if score is not None:
        conn.execute("INSERT INTO applicants (id, name, profile_path) "
                     "VALUES (1, 'u', 'p') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO matches (posting_id, applicant_id, score,"
                     " created_at) VALUES (?, 1, ?, datetime('now'))", (pid, score))


def seed(conn):
    for pid, title, score in [(1, "Trainee Preschooler", 9),
                              (2, "Junior Tooth Fairy", None),
                              (3, "Sandcastle Engineer", 4),
                              (4, "Cloud Watcher", None),
                              (5, "Nap Consultant", None)]:
        _posting(conn, pid, title, score)
    _sight(conn, 1, "greenhouse", "2026-07-20T06:00:00")
    _sight(conn, 1, "adzuna", "2026-07-20T09:00:00")
    _sight(conn, 1, "reed", "2026-07-21T09:00:00")
    _sight(conn, 2, "adzuna", "2026-07-21T06:00:00")
    _sight(conn, 2, "reed", "2026-07-22T06:00:00")
    _sight(conn, 3, "adzuna", "2026-07-22T06:00:00")
    _sight(conn, 4, "reed", "2026-07-22T07:00:00")
    _sight(conn, 5, "greenhouse", "2026-07-22T08:00:00")
    conn.commit()


def test_source_summary(conn):
    seed(conn)
    by = {s["source"]: s for s in source_analytics.source_summary(conn)}
    assert by["adzuna"]["jobs"] == 3 and by["adzuna"]["unique"] == 1
    assert by["adzuna"]["uniqueness_pct"] == 33 and by["adzuna"]["duplicate_pct"] == 67
    assert by["reed"]["jobs"] == 3 and by["reed"]["unique"] == 1
    assert by["greenhouse"]["jobs"] == 2 and by["greenhouse"]["uniqueness_pct"] == 50
    # freshness: among shared jobs, greenhouse won job1, adzuna won job2
    assert by["greenhouse"]["first_seen_wins"] == 1
    assert by["adzuna"]["first_seen_wins"] == 1
    assert by["reed"]["first_seen_wins"] == 0
    # match quality flows through provenance
    assert by["greenhouse"]["matches"] == 1 and by["greenhouse"]["avg_score"] == 9
    assert by["adzuna"]["strong_matches"] == 1  # job1 scored 9; job3 scored 4


def test_overlap_matrix(conn):
    seed(conn)
    m = source_analytics.overlap_matrix(conn)
    assert m["sources"] == ["adzuna", "greenhouse", "reed"]
    assert m["rows"]["adzuna"]["reed"] == 67
    assert m["rows"]["reed"]["adzuna"] == 67
    assert m["rows"]["greenhouse"]["adzuna"] == 50
    assert m["rows"]["adzuna"]["greenhouse"] == 33
    assert m["rows"]["adzuna"]["adzuna"] is None


def test_volume_by_day(conn):
    seed(conn)
    v = source_analytics.volume_by_day(conn, days=365)
    assert "2026-07-22" in v["days"]
    i = v["days"].index("2026-07-22")
    assert v["series"]["adzuna"][i] == 1
    assert v["series"]["reed"][i] == 2   # job2 + job4 both first seen on the 22nd
    assert v["series"]["greenhouse"][i] == 1


def test_empty_db_is_fine(conn):
    assert source_analytics.source_summary(conn) == []
    m = source_analytics.overlap_matrix(conn)
    assert m["sources"] == []
    assert source_analytics.volume_by_day(conn)["days"] == []
