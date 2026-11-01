#!/usr/bin/env python3
"""Coleta diária dos insights do Instagram (Instagram API com login do Instagram).

Grava tudo em data/instagram.sqlite. Pensado para rodar 1x por dia via cron:

    python3 collector/ig_collect.py            # coleta normal
    python3 collector/ig_collect.py --check    # só testa o token e mostra a conta
    python3 collector/ig_collect.py --demographics   # força coleta de demografia

Só usa a biblioteca padrão. O token fica em .env (IG_ACCESS_TOKEN) e é renovado
automaticamente a cada 7 dias (token de 60 dias; a renovação exige >= 24 h de idade).
"""

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
DB_PATH = ROOT / "data" / "instagram.sqlite"
TZ = ZoneInfo("America/Sao_Paulo")

# Os dados chegam com até 48 h de atraso, então reprocessamos os últimos dias.
REPROCESS_DAYS = 3
# Insights de mídia mudam por um tempo depois da publicação; atualiza só as recentes.
MEDIA_REFRESH_DAYS = 90
TOKEN_REFRESH_EVERY = timedelta(days=7)

ACCOUNT_DAY_METRICS = [
    "reach", "views", "accounts_engaged", "total_interactions", "likes", "comments",
    "shares", "saves", "replies", "profile_links_taps",
    "follows_and_unfollows",  # só com 100+ seguidores
]
MEDIA_METRICS = {
    "FEED": ["reach", "views", "likes", "comments", "shares", "saved",
             "total_interactions", "follows", "profile_visits"],
    "REELS": ["reach", "views", "likes", "comments", "shares", "saved",
              "total_interactions", "ig_reels_avg_watch_time"],
    "STORY": ["reach", "views", "replies", "shares", "follows", "profile_visits",
              "navigation"],
}
DEMOGRAPHIC_BREAKDOWNS = ["age", "gender", "city"]


class ApiError(Exception):
    def __init__(self, payload):
        err = payload.get("error", payload)
        self.code = err.get("code")
        super().__init__(f"[{self.code}] {err.get('message', payload)}")


# --- .env -------------------------------------------------------------------

def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def save_token(token):
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out, found = [], False
    for line in lines:
        if line.strip().startswith("IG_ACCESS_TOKEN="):
            out.append(f"IG_ACCESS_TOKEN={token}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"IG_ACCESS_TOKEN={token}")
    ENV_PATH.write_text("\n".join(out) + "\n")
    ENV_PATH.chmod(0o600)


# --- API --------------------------------------------------------------------

class Client:
    def __init__(self, token, version=""):
        self.token = token
        self.base = "https://graph.instagram.com" + (f"/{version}" if version else "")

    def get(self, path, **params):
        params["access_token"] = self.token
        url = f"{self.base}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                payload = json.load(e)
            except ValueError:
                payload = {"error": {"message": f"HTTP {e.code}", "code": e.code}}
            raise ApiError(payload) from None

    def paged(self, path, max_items=100, **params):
        """Segue a paginação até `max_items` itens."""
        data = self.get(path, **params)
        items = list(data.get("data", []))
        while len(items) < max_items and data.get("paging", {}).get("next"):
            with urllib.request.urlopen(data["paging"]["next"], timeout=30) as resp:
                data = json.load(resp)
            items.extend(data.get("data", []))
        return items[:max_items]

    def insights(self, path, metrics, **params):
        """Pede todas as métricas juntas; se a API recusar, tenta uma a uma.

        Retorna (lista de itens de insight, lista de erros por métrica).
        """
        try:
            return self.get(path, metric=",".join(metrics), **params).get("data", []), []
        except ApiError:
            pass
        items, errors = [], []
        for m in metrics:
            try:
                items.extend(self.get(path, metric=m, **params).get("data", []))
            except ApiError as e:
                errors.append((m, str(e)))
        return items, errors


def insight_values(item):
    """Achata um item de insight em pares (nome_da_métrica, valor)."""
    name = item["name"]
    tv = item.get("total_value")
    if tv is not None:
        if "value" in tv:
            yield name, tv["value"]
        for bd in tv.get("breakdowns", []):
            for r in bd.get("results", []):
                yield f"{name}:{'/'.join(r.get('dimension_values', []))}", r.get("value")
    elif item.get("values"):
        v = item["values"][-1].get("value")
        if isinstance(v, dict):  # ex.: navigation
            for k, sub in v.items():
                yield f"{name}:{k}", sub
        else:
            yield name, v


# --- banco ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS account_snapshot (
    date TEXT PRIMARY KEY, followers_count INTEGER, follows_count INTEGER,
    media_count INTEGER, collected_at TEXT);
CREATE TABLE IF NOT EXISTS account_insights (
    date TEXT, metric TEXT, value REAL, collected_at TEXT, PRIMARY KEY (date, metric));
CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY, media_type TEXT, media_product_type TEXT, timestamp TEXT,
    permalink TEXT, caption TEXT, like_count INTEGER, comments_count INTEGER,
    updated_at TEXT);
CREATE TABLE IF NOT EXISTS media_insights (
    media_id TEXT, collected_date TEXT, metric TEXT, value REAL,
    PRIMARY KEY (media_id, collected_date, metric));
CREATE TABLE IF NOT EXISTS demographics (
    collected_date TEXT, breakdown TEXT, key TEXT, value INTEGER,
    PRIMARY KEY (collected_date, breakdown, key));
CREATE TABLE IF NOT EXISTS run_log (
    run_at TEXT, level TEXT, message TEXT);
"""


def open_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    return db


def log(db, level, message):
    print(f"[{level}] {message}", file=sys.stderr if level != "info" else sys.stdout)
    db.execute("INSERT INTO run_log VALUES (?, ?, ?)", (now_iso(), level, message))


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def get_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


# --- etapas -----------------------------------------------------------------

def maybe_refresh_token(db, client, force=False):
    last = get_meta(db, "token_refreshed_at")
    if not force and last and datetime.fromisoformat(last) > datetime.now(TZ) - TOKEN_REFRESH_EVERY:
        return
    if last is None and not force:
        # Primeira execução: o token pode ter menos de 24 h; só marca a data.
        set_meta(db, "token_refreshed_at", now_iso())
        return
    try:
        data = client.get("refresh_access_token", grant_type="ig_refresh_token")
    except ApiError as e:
        log(db, "warn", f"renovação do token falhou: {e}")
        return
    client.token = data["access_token"]
    save_token(client.token)
    set_meta(db, "token_refreshed_at", now_iso())
    days = int(data.get("expires_in", 0)) // 86400
    log(db, "info", f"token renovado (vale ~{days} dias)")


def collect_account(db, client):
    me = client.get("me", fields="user_id,username,followers_count,follows_count,media_count")
    today = datetime.now(TZ).date().isoformat()
    db.execute(
        "INSERT OR REPLACE INTO account_snapshot VALUES (?, ?, ?, ?, ?)",
        (today, me.get("followers_count"), me.get("follows_count"),
         me.get("media_count"), now_iso()))
    log(db, "info", f"@{me.get('username')}: {me.get('followers_count')} seguidores")
    return me


def day_bounds(d):
    start = datetime.combine(d, time.min, TZ)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def collect_account_insights(db, client, ig_id, days=REPROCESS_DAYS):
    today = datetime.now(TZ).date()
    for i in range(1, days + 1):
        d = today - timedelta(days=i)
        since, until = day_bounds(d)
        items, errors = client.insights(
            f"{ig_id}/insights", ACCOUNT_DAY_METRICS, period="day",
            metric_type="total_value", since=since, until=until)
        for item in items:
            for metric, value in insight_values(item):
                db.execute("INSERT OR REPLACE INTO account_insights VALUES (?, ?, ?, ?)",
                           (d.isoformat(), metric, value, now_iso()))
        for metric, err in errors:
            log(db, "warn", f"conta {d} {metric}: {err}")


def media_kind(m):
    if m.get("media_product_type") == "STORY":
        return "STORY"
    if m.get("media_product_type") == "REELS":
        return "REELS"
    return "FEED"


def save_media_insights(db, client, m):
    items, errors = client.insights(f"{m['id']}/insights", MEDIA_METRICS[media_kind(m)])
    today = datetime.now(TZ).date().isoformat()
    for item in items:
        for metric, value in insight_values(item):
            db.execute("INSERT OR REPLACE INTO media_insights VALUES (?, ?, ?, ?)",
                       (m["id"], today, metric, value))
    if errors and not items:
        log(db, "warn", f"mídia {m['id']} sem insights: {errors[0][1]}")
        return
    for metric, err in errors:
        log(db, "warn", f"mídia {m['id']} {metric}: {err}")


def collect_media(db, client, all_media=False):
    fields = ("id,caption,media_type,media_product_type,timestamp,permalink,"
              "like_count,comments_count")
    posts = client.paged("me/media", max_items=100, fields=fields, limit=50)
    stories = []
    try:
        stories = client.paged("me/stories", max_items=50, fields=fields)
    except ApiError as e:
        log(db, "warn", f"stories: {e}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=MEDIA_REFRESH_DAYS)
    refreshed = 0
    for m in posts + stories:
        db.execute(
            "INSERT OR REPLACE INTO media VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (m["id"], m.get("media_type"), m.get("media_product_type"), m.get("timestamp"),
             m.get("permalink"), m.get("caption"), m.get("like_count"),
             m.get("comments_count"), now_iso()))
        ts = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        if all_media or ts >= cutoff or media_kind(m) == "STORY":
            save_media_insights(db, client, m)
            refreshed += 1
    log(db, "info", f"{len(posts)} posts, {len(stories)} stories; insights de {refreshed} mídias")


def collect_demographics(db, client, ig_id):
    today = datetime.now(TZ).date().isoformat()
    for bd in DEMOGRAPHIC_BREAKDOWNS:
        try:
            data = client.get(f"{ig_id}/insights", metric="follower_demographics",
                              period="lifetime", timeframe="this_month",
                              metric_type="total_value", breakdown=bd)
        except ApiError as e:
            log(db, "warn", f"demografia {bd}: {e} (exige 100+ seguidores)")
            continue
        for item in data.get("data", []):
            for metric, value in insight_values(item):
                if ":" in metric:
                    db.execute("INSERT OR REPLACE INTO demographics VALUES (?, ?, ?, ?)",
                               (today, bd, metric.split(":", 1)[1], value))
    log(db, "info", "demografia coletada")


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="só testa o token e mostra a conta")
    ap.add_argument("--demographics", action="store_true", help="força coleta de demografia")
    ap.add_argument("--all-media", action="store_true",
                    help=f"atualiza insights de todas as mídias, não só dos últimos {MEDIA_REFRESH_DAYS} dias")
    ap.add_argument("--refresh-token", action="store_true", help="força renovação do token")
    ap.add_argument("--days", type=int, default=REPROCESS_DAYS,
                    help=f"dias de insights da conta a (re)processar (padrão {REPROCESS_DAYS}, máx. ~30)")
    args = ap.parse_args()

    env = load_env()
    token = env.get("IG_ACCESS_TOKEN")
    if not token:
        sys.exit(f"IG_ACCESS_TOKEN vazio. Copie .env.example para {ENV_PATH} e preencha.")
    client = Client(token, env.get("IG_API_VERSION", ""))

    if args.check:
        me = client.get("me", fields="user_id,username,account_type,followers_count,media_count")
        print(json.dumps(me, indent=2, ensure_ascii=False))
        return

    db = open_db()
    try:
        maybe_refresh_token(db, client, force=args.refresh_token)
        me = collect_account(db, client)
        ig_id = me.get("user_id") or me["id"]
        collect_account_insights(db, client, ig_id, days=args.days)
        collect_media(db, client, all_media=args.all_media)
        if args.demographics or date.today().weekday() == 0:  # segunda-feira
            collect_demographics(db, client, ig_id)
        db.commit()
    except ApiError as e:
        log(db, "error", str(e))
        db.commit()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
