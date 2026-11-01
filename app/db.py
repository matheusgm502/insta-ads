"""Banco local: esquema versionado, migrações e as definições do usuário.

Só biblioteca padrão (os scripts do coletor rodam sem ambiente virtual).

- A versão do esquema fica em `PRAGMA user_version`; `connect()` aplica as migrações
  pendentes, cada uma numa transação única, depois de copiar o banco para `backups/`.
- Tudo que é de uma conta carrega `account_id`; os dados de um post herdam a conta pelo
  `media_id`. As chaves estrangeiras são **impostas** em toda conexão (`foreign_keys=ON`):
  gravar um dado sem o pai, ou apagar um pai que ainda tem filhos, dá erro e não perde nada.
  Durante migrações ficam desligadas (reconstruir tabelas exige) e são conferidas no fim.
- Definições do usuário: brief e guardrails têm versão e estado (rascunho, confirmado,
  substituído); só o que está **confirmado** vale. Sem guardrails confirmados, nenhum
  gasto pode ser feito. Consentimentos são um registro que só cresce.
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("APP_DB", ROOT / "data" / "instagram.sqlite"))
DEFAULT_TZ = "America/Sao_Paulo"

STATUSES = ("draft", "confirmed", "superseded")
APPROVAL_ACTIONS = ("create_campaign", "activate_campaign", "increase_budget",
                    "change_audience", "change_creative")
CONSENT_PURPOSES = ("instagram_data_access", "local_media_analysis",
                    "llm_text_processing", "ads_account_access")
GUARDRAIL_FIELDS = {
    "currency", "monthly_budget_cents", "daily_budget_cents", "max_cost_per_follower_cents",
    "hard_stop_cost_per_follower_cents", "no_change_days", "checkpoint_days",
    "evaluation_days", "requires_approval", "pause_without_approval"}
PREFERENCE_FIELDS = {"channel", "address", "summary_frequency", "summary_weekday", "summary_hour"}


def now_iso():
    return datetime.now(ZoneInfo(DEFAULT_TZ)).isoformat(timespec="seconds")


# --- esquema (versão 1) -----------------------------------------------------

SCHEMA_V1 = [
    """CREATE TABLE account (
        id INTEGER PRIMARY KEY,
        ig_user_id TEXT UNIQUE,
        username TEXT,
        timezone TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disconnected')),
        created_at TEXT NOT NULL)""",
    """CREATE TABLE account_snapshot (
        account_id INTEGER NOT NULL REFERENCES account(id),
        date TEXT NOT NULL, followers_count INTEGER, follows_count INTEGER,
        media_count INTEGER, collected_at TEXT,
        PRIMARY KEY (account_id, date))""",
    """CREATE TABLE account_insights (
        account_id INTEGER NOT NULL REFERENCES account(id),
        date TEXT NOT NULL, metric TEXT NOT NULL, value REAL, collected_at TEXT,
        PRIMARY KEY (account_id, date, metric))""",
    """CREATE TABLE demographics (
        account_id INTEGER NOT NULL REFERENCES account(id),
        collected_date TEXT NOT NULL, breakdown TEXT NOT NULL, key TEXT NOT NULL, value INTEGER,
        PRIMARY KEY (account_id, collected_date, breakdown, key))""",
    """CREATE TABLE media (
        id TEXT PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES account(id),
        media_type TEXT, media_product_type TEXT, timestamp TEXT, permalink TEXT, caption TEXT,
        like_count INTEGER, comments_count INTEGER, updated_at TEXT)""",
    "CREATE INDEX idx_media_account ON media (account_id, timestamp)",
    """CREATE TABLE media_insights (
        media_id TEXT NOT NULL REFERENCES media(id),
        collected_date TEXT NOT NULL, metric TEXT NOT NULL, value REAL,
        PRIMARY KEY (media_id, collected_date, metric))""",
    """CREATE TABLE transcripts (
        media_id TEXT PRIMARY KEY REFERENCES media(id),
        model TEXT, language TEXT, duration REAL, hook TEXT, text TEXT, segments TEXT,
        transcribed_at TEXT)""",
    """CREATE TABLE transcript_skips (
        media_id TEXT PRIMARY KEY REFERENCES media(id), reason TEXT, checked_at TEXT)""",
    """CREATE TABLE visual_status (
        media_id TEXT PRIMARY KEY REFERENCES media(id),
        source TEXT, duration REAL, n_frames INTEGER, n_cuts INTEGER, model TEXT, done_at TEXT)""",
    """CREATE TABLE visual_frames (
        media_id TEXT NOT NULL REFERENCES media(id),
        idx INTEGER NOT NULL, t REAL, source TEXT, is_cut INTEGER, path TEXT,
        description TEXT, model TEXT, described_at TEXT,
        PRIMARY KEY (media_id, idx))""",
    """CREATE TABLE meta (
        account_id INTEGER NOT NULL REFERENCES account(id),
        key TEXT NOT NULL, value TEXT,
        PRIMARY KEY (account_id, key))""",
    """CREATE TABLE run_log (
        account_id INTEGER REFERENCES account(id),
        run_at TEXT, level TEXT, message TEXT)""",
    "CREATE INDEX idx_run_log_run_at ON run_log (run_at)",
    # --- definições do usuário
    """CREATE TABLE brief_version (
        id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES account(id),
        version INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'superseded')),
        source TEXT NOT NULL CHECK (source IN ('generated', 'edited')),
        content TEXT NOT NULL,
        created_at TEXT NOT NULL, confirmed_at TEXT,
        UNIQUE (account_id, version))""",
    """CREATE TABLE guardrail_set (
        id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES account(id),
        version INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'superseded')),
        currency TEXT NOT NULL DEFAULT 'BRL',
        monthly_budget_cents INTEGER CHECK (monthly_budget_cents IS NULL OR monthly_budget_cents >= 0),
        daily_budget_cents INTEGER CHECK (daily_budget_cents IS NULL OR daily_budget_cents >= 0),
        max_cost_per_follower_cents INTEGER
            CHECK (max_cost_per_follower_cents IS NULL OR max_cost_per_follower_cents >= 0),
        hard_stop_cost_per_follower_cents INTEGER
            CHECK (hard_stop_cost_per_follower_cents IS NULL OR hard_stop_cost_per_follower_cents >= 0),
        no_change_days INTEGER CHECK (no_change_days IS NULL OR no_change_days >= 0),
        checkpoint_days INTEGER CHECK (checkpoint_days IS NULL OR checkpoint_days >= 0),
        evaluation_days INTEGER CHECK (evaluation_days IS NULL OR evaluation_days >= 0),
        requires_approval TEXT NOT NULL DEFAULT '[]',
        pause_without_approval INTEGER NOT NULL DEFAULT 1 CHECK (pause_without_approval IN (0, 1)),
        created_at TEXT NOT NULL, confirmed_at TEXT,
        UNIQUE (account_id, version))""",
    """CREATE TABLE preference (
        account_id INTEGER PRIMARY KEY REFERENCES account(id),
        channel TEXT NOT NULL DEFAULT 'none' CHECK (channel IN ('whatsapp', 'email', 'none')),
        address TEXT,
        summary_frequency TEXT NOT NULL DEFAULT 'weekly'
            CHECK (summary_frequency IN ('weekly', 'biweekly', 'monthly')),
        summary_weekday INTEGER NOT NULL DEFAULT 0 CHECK (summary_weekday BETWEEN 0 AND 6),
        summary_hour INTEGER NOT NULL DEFAULT 9 CHECK (summary_hour BETWEEN 0 AND 23),
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE consent_log (
        id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES account(id),
        purpose TEXT NOT NULL, provider TEXT,
        granted INTEGER NOT NULL CHECK (granted IN (0, 1)),
        note TEXT, recorded_at TEXT NOT NULL)""",
]

# Tabelas do esquema antigo (uma conta só). Na migração são renomeadas, recriadas e copiadas.
LEGACY_TABLES = ["account_snapshot", "account_insights", "demographics", "media", "media_insights",
                 "transcripts", "transcript_skips", "visual_status", "visual_frames", "meta", "run_log"]
ACCOUNT_LEVEL = {"account_snapshot", "account_insights", "demographics", "media", "meta", "run_log"}


def _exists(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _columns(db, table):
    return [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]


def m001_multi_account(db):
    """Esquema versionado e multi-conta. Um banco antigo vira uma conta provisória,
    que o coletor completa (id e @) na próxima execução."""
    legacy = [t for t in LEGACY_TABLES if _exists(db, t)]
    for t in legacy:
        db.execute(f'ALTER TABLE "{t}" RENAME TO "_old_{t}"')
    for statement in SCHEMA_V1:
        db.execute(statement)
    has_data = any(db.execute(f'SELECT 1 FROM "_old_{t}" LIMIT 1').fetchone()
                   for t in legacy if t in ACCOUNT_LEVEL)
    default_account = (db.execute("INSERT INTO account (created_at) VALUES (?)", (now_iso(),)).lastrowid
                       if has_data else None)
    for t in legacy:
        old, new = _columns(db, f"_old_{t}"), _columns(db, t)
        common = [c for c in new if c in old]
        cols = ", ".join(f'"{c}"' for c in common)
        if "account_id" in new and "account_id" not in old and default_account:
            db.execute(f'INSERT INTO "{t}" (account_id, {cols}) SELECT ?, {cols} FROM "_old_{t}"',
                       (default_account,))
        else:
            db.execute(f'INSERT INTO "{t}" ({cols}) SELECT {cols} FROM "_old_{t}"')
        db.execute(f'DROP TABLE "_old_{t}"')
    problems = db.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"Migração abortada: {len(problems)} linha(s) violam chaves estrangeiras "
                           f"(ex.: {problems[0]}). O banco não foi alterado.")


MIGRATIONS = [m001_multi_account]   # posição i = versão i + 1
LATEST = len(MIGRATIONS)


def version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def migrate(db):
    """Aplica as migrações pendentes. `db` deve estar em modo autocommit (isolation_level=None)."""
    db.execute("PRAGMA foreign_keys = OFF")  # reconstruir tabelas exige; conferido ao final
    current = version(db)
    for target, fn in enumerate(MIGRATIONS, start=1):
        if target <= current:
            continue
        db.execute("BEGIN")
        try:
            fn(db)
            db.execute(f"PRAGMA user_version = {target}")
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise


def _backup(db, path):
    folder = Path(path).parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = folder / f"{Path(path).stem}-v{version(db)}-{stamp}.sqlite"
    dest = sqlite3.connect(target)
    try:
        db.backup(dest)
    finally:
        dest.close()
    return target


def connect(path=None, backup=True):
    """Abre o banco já na versão mais recente do esquema (copiando-o antes de migrar)."""
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    mig = sqlite3.connect(path, isolation_level=None)
    try:
        if version(mig) < LATEST:
            has_tables = mig.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            if backup and has_tables:
                _backup(mig, path)
            migrate(mig)
    finally:
        mig.close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    return db


# --- contas -----------------------------------------------------------------

def ensure_account(db, ig_user_id, username=None):
    """Devolve o id da conta; cria, ou completa a conta provisória deixada pela migração."""
    ig_user_id = str(ig_user_id)
    row = db.execute("SELECT id FROM account WHERE ig_user_id = ?", (ig_user_id,)).fetchone()
    if row:
        if username:
            db.execute("UPDATE account SET username = ? WHERE id = ?", (username, row[0]))
        return row[0]
    placeholder = db.execute("SELECT id FROM account WHERE ig_user_id IS NULL ORDER BY id LIMIT 1").fetchone()
    if placeholder:
        db.execute("UPDATE account SET ig_user_id = ?, username = ? WHERE id = ?",
                   (ig_user_id, username, placeholder[0]))
        return placeholder[0]
    return db.execute("INSERT INTO account (ig_user_id, username, created_at) VALUES (?, ?, ?)",
                      (ig_user_id, username, now_iso())).lastrowid


def default_account_id(db):
    """O id da conta, quando existe exatamente uma; senão, erro claro."""
    ids = [r[0] for r in db.execute("SELECT id FROM account ORDER BY id")]
    if len(ids) == 1:
        return ids[0]
    raise LookupError("Nenhuma conta cadastrada. Rode o coletor primeiro." if not ids
                      else "Há mais de uma conta; informe o account_id.")


# --- definições versionadas (brief e guardrails) ----------------------------

def _next_version(db, table, account_id):
    row = db.execute(f"SELECT MAX(version) FROM {table} WHERE account_id = ?", (account_id,)).fetchone()
    return (row[0] or 0) + 1


def _confirm(db, table, account_id, version_number):
    row = db.execute(f"SELECT status FROM {table} WHERE account_id = ? AND version = ?",
                     (account_id, version_number)).fetchone()
    if not row:
        raise LookupError(f"{table}: versão {version_number} não existe.")
    if row[0] != "draft":
        raise ValueError(f"{table}: só um rascunho pode ser confirmado (estado atual: {row[0]}).")
    db.execute(f"UPDATE {table} SET status = 'superseded' WHERE account_id = ? AND status = 'confirmed'",
               (account_id,))
    db.execute(f"UPDATE {table} SET status = 'confirmed', confirmed_at = ? "
               "WHERE account_id = ? AND version = ?", (now_iso(), account_id, version_number))


def save_brief(db, account_id, content, source="generated"):
    """Grava um rascunho novo do brief (JSON) e devolve o número da versão."""
    number = _next_version(db, "brief_version", account_id)
    db.execute("INSERT INTO brief_version (account_id, version, status, source, content, created_at) "
               "VALUES (?, ?, 'draft', ?, ?, ?)",
               (account_id, number, source, json.dumps(content, ensure_ascii=False), now_iso()))
    return number


def confirm_brief(db, account_id, version_number):
    _confirm(db, "brief_version", account_id, version_number)


def current_brief(db, account_id):
    """O brief confirmado mais recente (ou None)."""
    row = db.execute("SELECT version, content, confirmed_at FROM brief_version "
                     "WHERE account_id = ? AND status = 'confirmed'", (account_id,)).fetchone()
    return {"version": row[0], "content": json.loads(row[1]), "confirmed_at": row[2]} if row else None


def save_guardrails(db, account_id, **fields):
    """Grava um rascunho de guardrails. Valores em centavos; `requires_approval` é uma lista."""
    unknown = set(fields) - GUARDRAIL_FIELDS
    if unknown:
        raise ValueError(f"Campos desconhecidos: {sorted(unknown)}")
    if "requires_approval" in fields:
        actions = list(fields["requires_approval"])
        invalid = set(actions) - set(APPROVAL_ACTIONS)
        if invalid:
            raise ValueError(f"Ações desconhecidas em requires_approval: {sorted(invalid)}. "
                             f"Válidas: {APPROVAL_ACTIONS}")
        fields["requires_approval"] = json.dumps(actions)
    number = _next_version(db, "guardrail_set", account_id)
    columns = ["account_id", "version", "status", "created_at", *fields]
    values = [account_id, number, "draft", now_iso(), *fields.values()]
    db.execute(f"INSERT INTO guardrail_set ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", values)
    return number


def confirm_guardrails(db, account_id, version_number):
    _confirm(db, "guardrail_set", account_id, version_number)


def current_guardrails(db, account_id):
    """Os guardrails confirmados (ou None). Sem eles, nenhum gasto é permitido."""
    cur = db.execute("SELECT * FROM guardrail_set WHERE account_id = ? AND status = 'confirmed'", (account_id,))
    row = cur.fetchone()
    if not row:
        return None
    data = dict(zip([d[0] for d in cur.description], row))
    data["requires_approval"] = json.loads(data["requires_approval"])
    data["pause_without_approval"] = bool(data["pause_without_approval"])
    return data


# --- preferências e consentimento -------------------------------------------

def set_preference(db, account_id, **fields):
    unknown = set(fields) - PREFERENCE_FIELDS
    if unknown:
        raise ValueError(f"Campos desconhecidos: {sorted(unknown)}")
    db.execute("INSERT OR IGNORE INTO preference (account_id, updated_at) VALUES (?, ?)", (account_id, now_iso()))
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE preference SET {sets}, updated_at = ? WHERE account_id = ?",
                   [*fields.values(), now_iso(), account_id])


def get_preference(db, account_id):
    cur = db.execute("SELECT * FROM preference WHERE account_id = ?", (account_id,))
    row = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], row)) if row else None


def record_consent(db, account_id, purpose, granted, provider=None, note=None):
    """Registra uma autorização ou revogação. O registro só cresce: revogar é gravar `granted=False`."""
    if purpose not in CONSENT_PURPOSES:
        raise ValueError(f"Finalidade desconhecida: {purpose!r}. Válidas: {CONSENT_PURPOSES}")
    db.execute("INSERT INTO consent_log (account_id, purpose, provider, granted, note, recorded_at) "
               "VALUES (?, ?, ?, ?, ?, ?)", (account_id, purpose, provider, int(bool(granted)), note, now_iso()))


def has_consent(db, account_id, purpose, provider=None):
    """O último registro da finalidade (e do fornecedor, se houver) decide."""
    row = db.execute("SELECT granted FROM consent_log WHERE account_id = ? AND purpose = ? "
                     "AND COALESCE(provider, '') = COALESCE(?, '') ORDER BY id DESC LIMIT 1",
                     (account_id, purpose, provider)).fetchone()
    return bool(row and row[0])
