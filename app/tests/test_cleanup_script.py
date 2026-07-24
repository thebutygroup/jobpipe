"""cleanup_testruns purges only the prefix — real users and their
activation slots survive."""

import importlib.util
import pathlib


def _load():
    path = pathlib.Path(__file__).parents[1] / "scripts" / "cleanup_testruns.py"
    spec = importlib.util.spec_from_file_location("cleanup_testruns", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _signup_event(conn, ref, etype="signup_auto_activated"):
    conn.execute("INSERT INTO events (event_type, payload_json, created_at) "
                 "VALUES (?, ?, datetime('now'))", (etype, f'{{"user_ref": "{ref}"}}'))


def test_purge_removes_prefix_only(conn):
    mod = _load()
    for ref in ("testrun1", "testrun2", "maya"):
        conn.execute("INSERT INTO applicants (name, profile_path, user_ref, active) "
                     "VALUES (?, 'p', ?, 1)", (ref, ref))
        _signup_event(conn, ref)
    conn.commit()
    result = mod.purge(conn, "testrun")
    assert sorted(result["users_removed"]) == ["testrun1", "testrun2"]
    assert result["counter_events_cleared"] == 2
    # real user + her activation slot untouched
    remaining = [r["user_ref"] for r in conn.execute("SELECT user_ref FROM applicants")]
    assert remaining == ["maya"]
    left = conn.execute("SELECT COUNT(*) c FROM events "
                        "WHERE event_type='signup_auto_activated'").fetchone()["c"]
    assert left == 1


def test_purge_is_repeatable(conn):
    mod = _load()
    assert mod.purge(conn, "testrun") == {"users_removed": [],
                                          "counter_events_cleared": 0}
