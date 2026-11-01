"""Testes da análise de desempenho. Usam só dados sintéticos gerados aqui."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import analysis
from app import db as appdb



def new_db(path):
    """Banco no esquema real do app, com uma conta de teste."""
    db = appdb.connect(path)
    appdb.ensure_account(db, "conta-teste", "conta_teste")
    return db


def insert_media(db, pid, ptype, ts, caption=""):
    db.execute("INSERT INTO media (id, account_id, media_type, media_product_type, timestamp, permalink, "
               "caption, like_count, comments_count, updated_at) VALUES (?, 1, 'VIDEO', ?, ?, ?, ?, 0, 0, ?)",
               (pid, ptype, ts, f"https://example.test/{pid}", caption, ts))


BASE = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)  # uma segunda-feira


def add_post(db, pid, kind="REELS", reach=500, shares=10, hook=None, days=0, hours=0, caption="legenda"):
    ts = (BASE + timedelta(days=days, hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    insert_media(db, pid, kind, ts, caption)
    for metric, value in (("reach", reach), ("shares", shares), ("saved", 1)):
        db.execute("INSERT INTO media_insights VALUES (?,?,?,?)", (pid, "2026-03-30", metric, value))
    if hook is not None:
        db.execute("INSERT INTO transcripts VALUES (?,?,?,?,?,?,?,?)", (pid, "m", "pt", 30.0, hook, hook, "[]", ts))


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.sqlite"
    db = new_db(path)
    # 20 Reels: os 10 com pergunta no gancho alcançam muito mais que os outros 10
    for i in range(10):
        add_post(db, f"q{i}", reach=1000 + i * 10, hook="Você já pensou nisso?", days=i)
        add_post(db, f"n{i}", reach=400 + i * 10, hook="Hoje vou falar de um tema", days=i, hours=1)
    for i in range(6):
        add_post(db, f"f{i}", kind="FEED", reach=300 + i * 5, days=i, hours=2)
    db.commit()
    db.close()
    return path


def test_ratios_are_relative_to_the_type_median(db_path):
    posts = analysis.load_posts(db_path)
    analysis.compute_ratios(posts)
    reels = [p for p in posts if p.kind == "REELS"]
    assert max(p.ratios["reach"] for p in reels) > 1 > min(p.ratios["reach"] for p in reels)
    feed = [p for p in posts if p.kind == "FEED"]
    assert 0.8 < sum(p.ratios["reach"] for p in feed) / len(feed) < 1.2


def test_finds_the_planted_pattern_with_high_confidence(db_path):
    posts = analysis.load_posts(db_path)
    analysis.compute_ratios(posts)
    found = [r for r in analysis.find_patterns(posts)
             if r["feature"] == "hook_pergunta" and r["metric"] == "reach"]
    assert len(found) == 1
    r = found[0]
    assert r["direction"] == "acima" and r["lift"] > 1.5
    assert r["n_with"] == 10 and r["n_without"] == 10
    assert r["confidence"] == "alta"


def test_a_feature_with_small_groups_is_not_tested(db_path):
    posts = analysis.load_posts(db_path)
    analysis.compute_ratios(posts)
    # só 2 posts com texto na tela: abaixo do mínimo de 3 por lado
    for i, p in enumerate(posts):
        p.has_screen_text = i < 2
    keys = {r["feature"] for r in analysis.find_patterns(posts)}
    assert "texto_na_tela" not in keys


def test_viral_post_is_flagged_and_left_out_of_the_tests(db_path):
    db = sqlite3.connect(db_path)
    add_post(db, "viral", reach=500_000, hook="Você sabia?", days=3, hours=3)
    db.commit()
    db.close()
    posts = analysis.load_posts(db_path)
    analysis.compute_ratios(posts)
    assert [p.id for p in posts if p.outlier] == ["viral"]
    n_with = next(r["n_with"] for r in analysis.find_patterns(posts) if r["feature"] == "hook_pergunta")
    assert n_with == 10  # o viral não entrou na contagem


def test_posts_without_metrics_are_skipped(db_path):
    db = sqlite3.connect(db_path)
    ts = BASE.strftime("%Y-%m-%dT%H:%M:%S+0000")
    insert_media(db, "sem", "REELS", ts)
    db.commit()
    db.close()
    assert "sem" not in {p.id for p in analysis.load_posts(db_path)}


def test_timestamps_are_converted_to_the_configured_timezone(tmp_path):
    path = tmp_path / "tz.sqlite"
    db = new_db(path)
    ts = "2026-03-01T01:30:00+0000"  # domingo 01:30 UTC = sábado 22:30 em São Paulo
    insert_media(db, "a", "REELS", ts)
    db.execute("INSERT INTO media_insights VALUES ('a','d','reach',10)")
    db.commit()
    db.close()
    post = analysis.load_posts(path)[0]
    assert post.date.weekday() == 5 and post.date.hour == 22


def test_screen_text_ignores_negative_descriptions(tmp_path):
    path = tmp_path / "v.sqlite"
    db = new_db(path)
    ts = BASE.strftime("%Y-%m-%dT%H:%M:%S+0000")
    for pid, text in (("com", "Você já tentou?"), ("sem", "Nenhum")):
        insert_media(db, pid, "REELS", ts)
        db.execute("INSERT INTO media_insights VALUES (?,'d','reach',10)", (pid,))
        db.execute("INSERT INTO visual_status VALUES (?,'video',30,1,0,'m',?)", (pid, ts))
        db.execute("INSERT INTO visual_frames VALUES (?,0,0,'video',0,'p',?,'m',?)",
                   (pid, json.dumps({"texto_na_tela": text}), ts))
    db.commit()
    db.close()
    by_id = {p.id: p for p in analysis.load_posts(path)}
    assert by_id["com"].has_screen_text is True and by_id["sem"].has_screen_text is False


def test_report_renders_with_the_pattern_and_the_caveat(db_path):
    posts = analysis.load_posts(db_path)
    result = analysis.analyze(posts, days=60, now=BASE + timedelta(days=15))
    text = analysis.render(result, {p.id: p for p in posts})
    assert "Gancho em forma de pergunta" in text
    assert "não causas" in text
    assert "Posts dos últimos 60 dias" in text
