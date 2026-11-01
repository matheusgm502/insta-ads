"""Testes do banco: migração, contas e definições do usuário. Só dados sintéticos."""

import sqlite3

import pytest

from app import db as appdb

LEGACY = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE account_snapshot (date TEXT PRIMARY KEY, followers_count INTEGER, follows_count INTEGER,
    media_count INTEGER, collected_at TEXT);
CREATE TABLE account_insights (date TEXT, metric TEXT, value REAL, collected_at TEXT, PRIMARY KEY (date, metric));
CREATE TABLE media (id TEXT PRIMARY KEY, media_type TEXT, media_product_type TEXT, timestamp TEXT,
    permalink TEXT, caption TEXT, like_count INTEGER, comments_count INTEGER, updated_at TEXT);
CREATE TABLE media_insights (media_id TEXT, collected_date TEXT, metric TEXT, value REAL,
    PRIMARY KEY (media_id, collected_date, metric));
CREATE TABLE demographics (collected_date TEXT, breakdown TEXT, key TEXT, value INTEGER,
    PRIMARY KEY (collected_date, breakdown, key));
CREATE TABLE run_log (run_at TEXT, level TEXT, message TEXT);
CREATE TABLE transcripts (media_id TEXT PRIMARY KEY, model TEXT, language TEXT, duration REAL, hook TEXT,
    text TEXT, segments TEXT, transcribed_at TEXT);
CREATE TABLE transcript_skips (media_id TEXT PRIMARY KEY, reason TEXT, checked_at TEXT);
CREATE TABLE visual_status (media_id TEXT PRIMARY KEY, source TEXT, duration REAL, n_frames INTEGER,
    n_cuts INTEGER, model TEXT, done_at TEXT);
CREATE TABLE visual_frames (media_id TEXT, idx INTEGER, t REAL, source TEXT, is_cut INTEGER, path TEXT,
    description TEXT, model TEXT, described_at TEXT, PRIMARY KEY (media_id, idx));
INSERT INTO meta VALUES ('token_refreshed_at', '2026-10-25');
INSERT INTO account_snapshot VALUES ('2026-10-25', 100, 50, 3, 'x');
INSERT INTO account_insights VALUES ('2026-10-24', 'reach', 10, 'x'), ('2026-10-24', 'views', 20, 'x');
INSERT INTO media VALUES ('m1','VIDEO','REELS','2026-10-01T10:00:00+0000','l1','c1',1,2,'x'),
                         ('m2','IMAGE','FEED','2026-10-02T10:00:00+0000','l2','c2',3,4,'x');
INSERT INTO media_insights VALUES ('m1','2026-10-25','reach',500), ('m2','2026-10-25','reach',300);
INSERT INTO demographics VALUES ('2026-10-26','age','25-34',60);
INSERT INTO run_log VALUES ('x','info','ok');
INSERT INTO transcripts VALUES ('m1','medium','pt',30,'gancho','texto','[]','x');
INSERT INTO transcript_skips VALUES ('m2','sem arquivo','x');
INSERT INTO visual_status VALUES ('m1','video',30,2,1,'q','x');
INSERT INTO visual_frames VALUES ('m1',0,0.5,'video',0,'p','{}','q','x');
"""
COUNTS = {"account_snapshot": 1, "account_insights": 2, "media": 2, "media_insights": 2, "demographics": 1,
          "run_log": 1, "meta": 1, "transcripts": 1, "transcript_skips": 1, "visual_status": 1, "visual_frames": 1}


def legacy_db(path, sql=LEGACY):
    db = sqlite3.connect(path)
    db.executescript(sql)
    db.commit()
    db.close()


def counts(db):
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNTS}


def test_fresh_database_gets_the_latest_schema(tmp_path):
    db = appdb.connect(tmp_path / "novo.sqlite")
    assert appdb.version(db) == appdb.LATEST
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"account", "brief_version", "guardrail_set", "preference", "consent_log"} <= tables
    assert not (tmp_path / "backups").exists()  # nada para salvar num banco vazio


def test_legacy_database_is_migrated_without_losing_rows(tmp_path):
    path = tmp_path / "antigo.sqlite"
    legacy_db(path)
    db = appdb.connect(path)
    assert appdb.version(db) == appdb.LATEST
    assert counts(db) == COUNTS
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    # tudo que era da conta antiga agora pertence a uma conta provisória
    (account_id,) = db.execute("SELECT id FROM account").fetchone()
    assert db.execute("SELECT COUNT(*) FROM media WHERE account_id = ?", (account_id,)).fetchone()[0] == 2
    assert db.execute("SELECT value FROM meta WHERE account_id = ? AND key = 'token_refreshed_at'",
                      (account_id,)).fetchone()[0] == "2026-10-25"
    assert db.execute("SELECT ig_user_id FROM account").fetchone()[0] is None


def test_migration_keeps_a_backup_of_the_old_database(tmp_path):
    path = tmp_path / "antigo.sqlite"
    legacy_db(path)
    appdb.connect(path)
    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert len(backups) == 1
    old = sqlite3.connect(backups[0])
    assert old.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 2
    assert "account_id" not in [r[1] for r in old.execute("PRAGMA table_info(media)")]


def test_failed_migration_leaves_the_database_untouched(tmp_path):
    path = tmp_path / "quebrado.sqlite"
    legacy_db(path, LEGACY + "INSERT INTO media_insights VALUES ('orfao','2026-10-25','reach',1);")
    with pytest.raises(RuntimeError, match="chaves estrangeiras"):
        appdb.connect(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    assert "account_id" not in [r[1] for r in raw.execute("PRAGMA table_info(media)")]
    assert raw.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 2
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name LIKE '\\_old\\_%' ESCAPE '\\'").fetchall()


def test_migrating_twice_is_a_no_op(tmp_path):
    path = tmp_path / "antigo.sqlite"
    legacy_db(path)
    appdb.connect(path).close()
    db = appdb.connect(path)
    assert counts(db) == COUNTS
    assert len(list((tmp_path / "backups").glob("*.sqlite"))) == 1


def test_ensure_account_completes_the_placeholder_then_is_idempotent(tmp_path):
    path = tmp_path / "antigo.sqlite"
    legacy_db(path)
    db = appdb.connect(path)
    first = appdb.ensure_account(db, 12345, "conta_a")
    assert db.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 1
    assert appdb.ensure_account(db, "12345", "conta_a_renomeada") == first
    assert db.execute("SELECT username FROM account WHERE id = ?", (first,)).fetchone()[0] == "conta_a_renomeada"
    second = appdb.ensure_account(db, 999, "conta_b")
    assert second != first


def test_two_accounts_can_have_the_same_day_without_colliding(tmp_path):
    db = appdb.connect(tmp_path / "x.sqlite")
    a, b = appdb.ensure_account(db, "a"), appdb.ensure_account(db, "b")
    for acc, followers in ((a, 100), (b, 900)):
        db.execute("INSERT INTO account_snapshot (account_id, date, followers_count) VALUES (?, '2026-11-01', ?)",
                   (acc, followers))
    assert db.execute("SELECT COUNT(*) FROM account_snapshot").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO account_snapshot (account_id, date) VALUES (?, '2026-11-01')", (a,))
    with pytest.raises(LookupError, match="mais de uma conta"):
        appdb.default_account_id(db)


def test_default_account_id_errors_when_there_is_none(tmp_path):
    db = appdb.connect(tmp_path / "x.sqlite")
    with pytest.raises(LookupError, match="Nenhuma conta"):
        appdb.default_account_id(db)


class Ctx:
    """Conexão de teste com uma conta já criada (`conn.account`)."""

    def __init__(self, db, account):
        self._db, self.account = db, account

    def __getattr__(self, name):
        return getattr(self._db, name)


@pytest.fixture
def conn(tmp_path):
    db = appdb.connect(tmp_path / "x.sqlite")
    return Ctx(db, appdb.ensure_account(db, "a", "conta_teste"))


def test_brief_has_versions_and_only_a_confirmed_one_counts(conn):
    assert appdb.current_brief(conn._db, conn.account) is None
    v1 = appdb.save_brief(conn._db, conn.account, {"tom": "acolhedor"})
    assert v1 == 1 and appdb.current_brief(conn._db, conn.account) is None  # rascunho não vale
    appdb.confirm_brief(conn._db, conn.account, v1)
    assert appdb.current_brief(conn._db, conn.account)["content"] == {"tom": "acolhedor"}
    v2 = appdb.save_brief(conn._db, conn.account, {"tom": "direto"}, source="edited")
    appdb.confirm_brief(conn._db, conn.account, v2)
    assert appdb.current_brief(conn._db, conn.account)["version"] == 2
    statuses = [r[0] for r in conn.execute("SELECT status FROM brief_version ORDER BY version")]
    assert statuses == ["superseded", "confirmed"]
    with pytest.raises(ValueError):
        appdb.confirm_brief(conn._db, conn.account, v1)  # só rascunho pode ser confirmado


def test_guardrails_are_enforced_only_when_confirmed(conn):
    assert appdb.current_guardrails(conn._db, conn.account) is None  # sem teto, sem gasto
    v = appdb.save_guardrails(conn._db, conn.account, monthly_budget_cents=50000, max_cost_per_follower_cents=400,
                              requires_approval=["activate", "change_budget"])
    assert appdb.current_guardrails(conn._db, conn.account) is None
    appdb.confirm_guardrails(conn._db, conn.account, v)
    g = appdb.current_guardrails(conn._db, conn.account)
    assert g["monthly_budget_cents"] == 50000 and g["currency"] == "BRL"
    assert g["requires_approval"] == ["activate", "change_budget"] and g["pause_without_approval"] is True


def test_guardrails_reject_unknown_fields_and_negative_amounts(conn):
    with pytest.raises(ValueError, match="desconhecidos"):
        appdb.save_guardrails(conn._db, conn.account, teto_magico=1)
    with pytest.raises(sqlite3.IntegrityError):
        appdb.save_guardrails(conn._db, conn.account, monthly_budget_cents=-1)


def test_preferences_are_upserted_and_validated(conn):
    appdb.set_preference(conn._db, conn.account, channel="whatsapp", address="+55 00 00000-0000")
    appdb.set_preference(conn._db, conn.account, summary_weekday=4)
    p = appdb.get_preference(conn._db, conn.account)
    assert p["channel"] == "whatsapp" and p["summary_weekday"] == 4 and p["summary_frequency"] == "weekly"
    with pytest.raises(sqlite3.IntegrityError):
        appdb.set_preference(conn._db, conn.account, channel="pombo-correio")


def test_consent_log_only_grows_and_the_latest_record_wins(conn):
    assert not appdb.has_consent(conn._db, conn.account, "llm_text_processing", "openai")
    appdb.record_consent(conn._db, conn.account, "llm_text_processing", True, provider="openai")
    assert appdb.has_consent(conn._db, conn.account, "llm_text_processing", "openai")
    assert not appdb.has_consent(conn._db, conn.account, "llm_text_processing", "deepseek")  # por fornecedor
    appdb.record_consent(conn._db, conn.account, "llm_text_processing", False, provider="openai", note="revogou")
    assert not appdb.has_consent(conn._db, conn.account, "llm_text_processing", "openai")
    assert conn.execute("SELECT COUNT(*) FROM consent_log").fetchone()[0] == 2
    with pytest.raises(ValueError, match="Finalidade"):
        appdb.record_consent(conn._db, conn.account, "outra_coisa", True)
