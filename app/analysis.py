#!/usr/bin/env python3
"""Análise de desempenho dos posts, sem IA.

Compara cada post com a mediana do seu tipo (Reels ou feed) e procura padrões nos
atributos do post (gancho, formato, duração, cortes, texto na tela, horário).
Poucos dados: o resultado são "padrões observados" com nível de confiança, nunca
relações de causa e efeito. Posts muito fora da curva ficam de fora dos testes.

    python3 -m app.analysis                 # janela de 30 dias, grava reports/analise.md
    python3 -m app.analysis --days 14 --out /caminho/analise.md

Lê só o banco local (APP_DB, padrão data/instagram.sqlite). O fuso vem de APP_TZ.
"""

import argparse
import math
import os
import random
import re
import sqlite3
import statistics
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import db as appdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("APP_DB", ROOT / "data" / "instagram.sqlite"))
TZ = ZoneInfo(os.environ.get("APP_TZ", "America/Sao_Paulo"))

METRICS = {"reach": "Alcance", "shares": "Compartilhamentos", "saved": "Salvamentos"}
OUTLIER_RATIO = 10.0     # alcance >= 10x a mediana do tipo = fora da curva
ABOVE, BELOW = 1.5, 1 / 1.5
MIN_GROUP = 3            # mínimo de posts em cada lado para testar um padrão
PERMUTATIONS = 5000
INTERROGATIVES = {"quem", "qual", "quais", "quando", "onde", "como", "quanto", "quantos", "será"}
NO_TEXT = {"", "nenhum", "nenhuma", "não", "nao", "nada", "n/a", "não há", "nao ha"}


@dataclass
class Post:
    id: str
    date: datetime
    kind: str                       # REELS ou FEED
    permalink: str
    caption: str
    metrics: dict
    hook: str | None = None
    duration: float | None = None
    n_cuts: int | None = None
    has_screen_text: bool | None = None
    speech: str | None = None
    ratios: dict = field(default_factory=dict)   # métrica -> valor / mediana do tipo
    outlier: bool = False


@dataclass
class Feature:
    key: str
    label: str
    test: object                    # Post -> bool | None


# --- leitura ----------------------------------------------------------------

def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _has_text(value):
    v = (value or "").strip().lower().rstrip(".")
    return v not in NO_TEXT and not v.startswith("não há") and not v.startswith("nao ha")


def load_posts(db_path=None):
    db = appdb.connect(db_path or DB_PATH)
    account_id = appdb.default_account_id(db)
    latest = {}
    for media_id, metric, value in db.execute("""
            SELECT i.media_id, i.metric, i.value FROM media_insights i
            JOIN (SELECT media_id, MAX(collected_date) d FROM media_insights GROUP BY media_id) l
              ON l.media_id = i.media_id AND l.d = i.collected_date"""):
        latest.setdefault(media_id, {})[metric] = value
    hooks, cuts, texts = {}, {}, {}
    if _has_table(db, "transcripts"):
        hooks = {r[0]: (r[1], r[2], r[3]) for r in db.execute("SELECT media_id, hook, duration, text FROM transcripts")}
    if _has_table(db, "visual_status"):
        cuts = {r[0]: r[1] for r in db.execute("SELECT media_id, n_cuts FROM visual_status WHERE source = 'video'")}
        for media_id, desc in db.execute("SELECT media_id, description FROM visual_frames WHERE description IS NOT NULL"):
            try:
                found = _has_text(json.loads(desc).get("texto_na_tela"))
            except (ValueError, AttributeError):
                continue
            texts[media_id] = texts.get(media_id, False) or found
    posts = []
    for mid, ptype, ts, link, cap in db.execute(
            "SELECT id, media_product_type, timestamp, permalink, caption FROM media WHERE account_id = ?",
            (account_id,)):
        kind = {"REELS": "REELS", "FEED": "FEED"}.get(ptype)
        metrics = latest.get(mid)
        if not kind or not metrics or metrics.get("reach") is None:
            continue
        hook, duration, speech = hooks.get(mid, (None, None, None))
        posts.append(Post(
            id=mid, date=datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(TZ), kind=kind,
            permalink=link or "", caption=cap or "", metrics=metrics, hook=hook, duration=duration, speech=speech,
            n_cuts=cuts.get(mid), has_screen_text=texts.get(mid)))
    db.close()
    return sorted(posts, key=lambda p: p.date)


# --- cálculo ----------------------------------------------------------------

def compute_ratios(posts):
    """Razão de cada métrica contra a mediana do tipo; marca os fora da curva."""
    for kind in ("REELS", "FEED"):
        group = [p for p in posts if p.kind == kind]
        for metric in METRICS:
            values = [p.metrics.get(metric) or 0 for p in group]
            if not values:
                continue
            med = statistics.median(values)
            for p in group:
                p.ratios[metric] = ((p.metrics.get(metric) or 0) + 1) / (med + 1)
        for p in group:
            p.outlier = p.ratios.get("reach", 1) >= OUTLIER_RATIO


def _question(hook):
    h = hook.strip().lower()
    return "?" in h or (bool(h) and h.split()[0] in INTERROGATIVES)


def _direct(hook):
    return re.search(r"\b(você|voce|vc|tu|teu|tua|seu|sua|contigo)\b", hook.lower()) is not None


def build_features(posts):
    med = statistics.median
    caption_med = med(len(p.caption) for p in posts) if posts else 0
    hook_words = [len(p.hook.split()) for p in posts if p.hook]
    hook_med = med(hook_words) if hook_words else None
    durations = [p.duration for p in posts if p.duration]
    duration_med = med(durations) if durations else None
    f = [
        Feature("hook_pergunta", "Gancho em forma de pergunta",
                lambda p: _question(p.hook) if p.hook is not None else None),
        Feature("hook_direto", "Gancho fala direto com o público (você, seu, sua)",
                lambda p: _direct(p.hook) if p.hook is not None else None),
        Feature("legenda_longa", f"Legenda longa (mais de {caption_med:g} caracteres)",
                lambda p: len(p.caption) > caption_med),
        Feature("texto_na_tela", "Texto sobreposto no vídeo", lambda p: p.has_screen_text),
        Feature("muitos_cortes", "Vídeo com 2 ou mais cortes de cena",
                lambda p: None if p.n_cuts is None else p.n_cuts >= 2),
        Feature("fim_de_semana", "Publicado no fim de semana", lambda p: p.date.weekday() >= 5),
        Feature("noite", "Publicado a partir das 18h", lambda p: p.date.hour >= 18),
    ]
    if hook_med:
        f.append(Feature("hook_curto", f"Gancho curto (até {hook_med:g} palavras)",
                         lambda p: None if not p.hook else len(p.hook.split()) <= hook_med))
    if duration_med:
        f.append(Feature("duracao_longa", f"Vídeo longo (mais de {duration_med:.0f} s)",
                         lambda p: None if not p.duration else p.duration > duration_med))
    return f


def permutation_p(a, b, n=PERMUTATIONS, seed=7):
    """p-valor (bicaudal) da diferença entre as medianas dos dois grupos."""
    observed = abs(statistics.median(a) - statistics.median(b))
    pool, k, rng, hits = list(a) + list(b), len(a), random.Random(seed), 0
    for _ in range(n):
        rng.shuffle(pool)
        if abs(statistics.median(pool[:k]) - statistics.median(pool[k:])) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (n + 1)


def confidence(p, n_min):
    if p < 0.05 and n_min >= 8:
        return "alta"
    if p < 0.10 and n_min >= 5:
        return "média"
    return "baixa"


def find_patterns(posts):
    """Um resultado por (atributo, métrica) com pelo menos MIN_GROUP posts de cada lado."""
    usable = [p for p in posts if not p.outlier]
    results = []
    for feat in build_features(usable):
        marks = [(p, feat.test(p)) for p in usable]
        with_f = [p for p, m in marks if m is True]
        without = [p for p, m in marks if m is False]
        if len(with_f) < MIN_GROUP or len(without) < MIN_GROUP:
            continue
        for metric, metric_label in METRICS.items():
            a = [math.log(p.ratios[metric]) for p in with_f]
            b = [math.log(p.ratios[metric]) for p in without]
            p_value = permutation_p(a, b)
            lift = math.exp(statistics.median(a) - statistics.median(b))
            examples = sorted(with_f, key=lambda p: -p.ratios[metric])[:3]
            results.append({
                "feature": feat.key, "label": feat.label, "metric": metric, "metric_label": metric_label,
                "n_with": len(with_f), "n_without": len(without), "lift": lift, "p": p_value,
                "confidence": confidence(p_value, min(len(with_f), len(without))),
                "direction": "acima" if lift >= 1 else "abaixo",
                "examples": [p.id for p in examples]})
    return sorted(results, key=lambda r: r["p"])


def tier(post):
    if post.outlier:
        return "fora da curva"
    r = post.ratios.get("reach", 1)
    return "acima" if r >= ABOVE else "abaixo" if r <= BELOW else "na média"


def analyze(posts, days=30, now=None):
    compute_ratios(posts)
    now = now or datetime.now(TZ)
    window = [p for p in posts if p.date >= now - timedelta(days=days)]
    medians = {kind: {m: statistics.median([p.metrics.get(m) or 0 for p in posts if p.kind == kind])
                      for m in METRICS} for kind in ("REELS", "FEED") if any(p.kind == kind for p in posts)}
    return {"n_posts": len(posts), "medians": medians, "window_days": days, "window": window,
            "outliers": [p for p in posts if p.outlier], "patterns": find_patterns(posts)}


# --- relatório --------------------------------------------------------------

def _n(v):
    return f"{v:,.0f}".replace(",", ".")


def _caption(p, n=60):
    c = " ".join((p.hook or p.caption or "(sem texto)").split())
    return c[:n] + ("…" if len(c) > n else "")


def render(result, posts_by_id):
    L = ["# Análise de desempenho", "",
         f"Gerado em {datetime.now(TZ):%d/%m/%Y %H:%M}. {result['n_posts']} posts com métricas.",
         "Cada post é comparado com a mediana do seu tipo. Os padrões abaixo são **observações**, "
         "não causas: com poucos posts, qualquer coincidência pode parecer um padrão.", ""]
    L += ["## Mediana por tipo", "", "| Tipo | " + " | ".join(METRICS.values()) + " |", "|---|" + "---:|" * len(METRICS)]
    for kind, label in (("REELS", "Reels"), ("FEED", "Feed")):
        if kind in result["medians"]:
            L.append(f"| {label} | " + " | ".join(_n(result["medians"][kind][m]) for m in METRICS) + " |")
    L.append("")

    win = sorted(result["window"], key=lambda p: -p.ratios.get("reach", 0))
    L += [f"## Posts dos últimos {result['window_days']} dias", ""]
    if not win:
        L += ["Nenhum post no período.", ""]
    else:
        L += ["| Data | Tipo | Alcance | vs. mediana | Situação | Post |", "|---|---|---:|---:|---|---|"]
        for p in win:
            L.append(f"| {p.date:%d/%m} | {'Reels' if p.kind == 'REELS' else 'Feed'} | {_n(p.metrics['reach'])} | "
                     f"{p.ratios['reach']:.1f}x | {tier(p)} | [{_caption(p)}]({p.permalink}) |")
        L.append("")

    if result["outliers"]:
        L += ["## Fora da curva", "",
              f"Alcance de {OUTLIER_RATIO:g}x a mediana do tipo ou mais. Ficam de fora dos testes de padrão "
              "para não distorcer a conta.", ""]
        for p in result["outliers"]:
            L.append(f"- {p.date:%d/%m/%Y}: alcance {_n(p.metrics['reach'])} ({p.ratios['reach']:.0f}x). "
                     f"[{_caption(p)}]({p.permalink})")
        L.append("")

    shown = [r for r in result["patterns"] if r["confidence"] != "baixa" or r["p"] < 0.15]
    L += ["## Padrões observados", ""]
    if not shown:
        L += ["Nenhum padrão se destacou com os dados atuais.", ""]
    else:
        L += ["| Atributo | Métrica | Com | Sem | Diferença | p | Confiança |", "|---|---|---:|---:|---:|---:|---|"]
        for r in shown:
            L.append(f"| {r['label']} | {r['metric_label']} | {r['n_with']} | {r['n_without']} | "
                     f"{r['lift']:.1f}x ({r['direction']}) | {r['p']:.3f} | {r['confidence']} |")
        L += ["", "\"Diferença\" é a razão entre as medianas dos posts com e sem o atributo, já descontado o tipo. "
              "Foram testados vários atributos e métricas ao mesmo tempo, então alguns resultados aparecem por acaso; "
              "só confiança **alta** merece atenção, e mesmo assim como hipótese a testar.", "", "### Exemplos", ""]
        for r in shown[:6]:
            ex = [posts_by_id[i] for i in r["examples"] if i in posts_by_id]
            L.append(f"- **{r['label']}** ({r['metric_label'].lower()}): " +
                     "; ".join(f"[{p.date:%d/%m}]({p.permalink})" for p in ex))
        L.append("")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=30, help="janela de posts recentes (padrão 30)")
    ap.add_argument("--db", type=Path, help="banco SQLite (padrão APP_DB)")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "analise.md")
    args = ap.parse_args()
    posts = load_posts(args.db)
    if not posts:
        raise SystemExit("Nenhum post com métricas no banco. Rode o coletor antes.")
    result = analyze(posts, days=args.days)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(result, {p.id: p for p in posts}))
    strong = sum(1 for r in result["patterns"] if r["confidence"] == "alta")
    print(f"{len(posts)} posts, {len(result['outliers'])} fora da curva, "
          f"{strong} padrões com confiança alta. Relatório em {args.out}")


if __name__ == "__main__":
    main()
