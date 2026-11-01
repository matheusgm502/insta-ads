"""O coletor grava na conta certa. Usa um cliente falso: sem rede e sem token."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import ig_collect  # noqa: E402
from app import db as appdb  # noqa: E402


class FakeClient:
    token = "t"

    def __init__(self, ig_id, username, followers):
        self.me = {"user_id": ig_id, "username": username, "followers_count": followers,
                   "follows_count": 5, "media_count": 1}

    def get(self, path, **params):
        return self.me

    def insights(self, path, metrics, **params):
        return [{"name": "reach", "total_value": {"value": 42}}], []

    def paged(self, path, max_items=100, **params):
        if path == "me/stories":
            return []
        return [{"id": f"{self.me['user_id']}-post", "media_type": "VIDEO", "media_product_type": "REELS",
                 "timestamp": "2026-10-01T10:00:00+0000", "permalink": "l", "caption": "c",
                 "like_count": 1, "comments_count": 0}]


def run_once(db, client):
    me, account_id = ig_collect.collect_account(db, client)
    ig_collect.collect_account_insights(db, client, me["user_id"], account_id, days=1)
    ig_collect.collect_media(db, client, account_id)
    ig_collect.maybe_refresh_token(db, client, account_id)
    db.commit()
    return account_id


def test_two_accounts_are_collected_into_their_own_rows(tmp_path):
    db = appdb.connect(tmp_path / "x.sqlite")
    a = run_once(db, FakeClient("111", "conta_a", 100))
    b = run_once(db, FakeClient("222", "conta_b", 900))
    assert a != b
    snapshots = dict(db.execute("SELECT account_id, followers_count FROM account_snapshot"))
    assert snapshots == {a: 100, b: 900}
    assert dict(db.execute("SELECT id, account_id FROM media")) == {"111-post": a, "222-post": b}
    assert db.execute("SELECT COUNT(*) FROM account_insights WHERE account_id = ?", (a,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM media_insights").fetchone()[0] == 2
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_token_refresh_marker_is_kept_per_account(tmp_path):
    db = appdb.connect(tmp_path / "x.sqlite")
    a = run_once(db, FakeClient("111", "conta_a", 1))
    b = run_once(db, FakeClient("222", "conta_b", 1))
    assert ig_collect.get_meta(db, a, "token_refreshed_at") is not None
    ig_collect.set_meta(db, b, "token_refreshed_at", "2020-01-01")
    assert ig_collect.get_meta(db, a, "token_refreshed_at") != "2020-01-01"
