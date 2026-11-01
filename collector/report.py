#!/usr/bin/env python3
"""Relatório do Instagram: compara um período base com um período atual.

Puxa tudo direto da API (só leitura) e grava um Markdown em reports/:

    python3 collector/report.py                                  # base = agosto, atual = últimos 30 dias
    python3 collector/report.py --base 2026-07                   # base = julho
    python3 collector/report.py --base-start 2026-08-01 --base-end 2026-08-15
    python3 collector/report.py --current-start 2026-10-01 --current-end 2026-10-24

Os relatórios contêm dados da conta e ficam fora do git (reports/ está no .gitignore).
"""

import argparse
import calendar
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from ig_collect import ROOT, TZ, ApiError, Client, day_bounds, insight_values, load_env, media_kind

# Período base padrão: mês de agosto do ano corrente. Troque aqui ou use --base/--base-start/--base-end.
DEFAULT_BASE_MONTH = f"{date.today().year}-08"
# Período atual padrão: últimos N dias completos (ontem para trás).
DEFAULT_CURRENT_DAYS = 30
REPORTS_DIR = ROOT / "reports"
# A API limita algumas consultas a janelas de ~30 dias; períodos maiores são divididos.
MAX_WINDOW_DAYS = 30

PERIOD_METRICS = [
    ("reach", "Alcance (contas únicas)"),
    ("views", "Visualizações"),
    ("accounts_engaged", "Contas engajadas"),
    ("total_interactions", "Interações"),
    ("likes", "Curtidas"),
    ("comments", "Comentários"),
    ("shares", "Compartilhamentos"),
    ("saves", "Salvamentos"),
    ("replies", "Respostas a Stories"),
    ("profile_links_taps", "Cliques nos links do perfil"),
]
POST_METRICS = ["reach", "views", "likes", "comments", "shares", "saved", "follows",
                "profile_visits"]
KIND_LABEL = {"FEED": "Feed", "REELS": "Reels", "STORY": "Story"}


@dataclass
class Period:
    name: str
    start: date
    end: date  # inclusivo
    totals: dict = field(default_factory=dict)
    reach_is_sum: bool = False
    new_followers: list = field(default_factory=list)  # [(data, novos seguidores)]
    posts: list = field(default_factory=list)  # [{..., "insights": {...}}]

    @property
    def days(self):
        return (self.end - self.start).days + 1

    @property
    def label(self):
        return f"{self.start:%d/%m/%Y} a {self.end:%d/%m/%Y} ({self.days} dias)"


# --- períodos ---------------------------------------------------------------

def month_range(spec):
    y, m = map(int, spec.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def windows(start, end, size=MAX_WINDOW_DAYS):
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=size - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def bounds(start, end):
    return day_bounds(start)[0], day_bounds(end)[1]


# --- coleta -----------------------------------------------------------------

def fetch_totals(client, ig_id, p):
    """Totais do período. Tenta uma consulta só (alcance deduplicado); se a API
    recusar a janela, soma janelas de 30 dias (o alcance vira soma, não único)."""
    metrics = [m for m, _ in PERIOD_METRICS]

    def query(a, b):
        since, until = bounds(a, b)
        items, _ = client.insights(f"{ig_id}/insights", metrics, period="day",
                                   metric_type="total_value", since=since, until=until)
        return [kv for it in items for kv in insight_values(it) if ":" not in kv[0]]

    try:
        p.totals = dict(query(p.start, p.end))
        if p.totals:
            return
    except ApiError:
        pass
    p.reach_is_sum = True
    p.totals = {}
    for a, b in windows(p.start, p.end):
        for name, value in query(a, b):
            if value is not None:
                p.totals[name] = p.totals.get(name, 0) + value


def fetch_new_followers(client, ig_id, p):
    for a, b in windows(p.start, p.end):
        # A API marca cada dia pelo fim dele (meia-noite do Pacífico); pede um dia a mais.
        since, until = bounds(a, b + timedelta(days=1))
        try:
            data = client.get(f"{ig_id}/insights", metric="follower_count", period="day",
                              since=since, until=until)
        except ApiError as e:
            print(f"[warn] novos seguidores {a}–{b}: {e}", file=sys.stderr)
            continue
        for item in data.get("data", []):
            for v in item.get("values", []):
                d = date.fromisoformat(v["end_time"][:10]) - timedelta(days=1)
                if p.start <= d <= p.end:
                    p.new_followers.append((d, v.get("value") or 0))


def fetch_media(client):
    fields = "id,caption,media_type,media_product_type,timestamp,permalink"
    return client.paged("me/media", max_items=500, fields=fields, limit=50)


def fetch_post_insights(client, post):
    items, _ = client.insights(f"{post['id']}/insights", POST_METRICS)
    return {k: v for it in items for k, v in insight_values(it) if ":" not in k}


def assign_posts(client, periods, media):
    for m in media:
        ts = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(TZ)
        for p in periods:
            if p.start <= ts.date() <= p.end:
                m["date"] = ts.date()
                m["insights"] = fetch_post_insights(client, m)
                p.posts.append(m)


def fetch_demographics(client, ig_id):
    out = {}
    for bd in ("age", "gender", "city"):
        try:
            data = client.get(f"{ig_id}/insights", metric="follower_demographics",
                              period="lifetime", timeframe="this_month",
                              metric_type="total_value", breakdown=bd)
        except ApiError:
            continue
        rows = [(k.split(":", 1)[1], v) for it in data.get("data", [])
                for k, v in insight_values(it) if ":" in k]
        out[bd] = sorted(rows, key=lambda r: -r[1])
    return out


# --- formatação -------------------------------------------------------------

def fmt(v, decimals=0):
    if v is None:
        return "–"
    s = f"{v:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def delta(base, cur):
    if base in (None, 0) or cur is None:
        return "–"
    pct = (cur - base) / base * 100
    return f"{'+' if pct >= 0 else ''}{fmt(pct, 0)}%"


def caption(m, n=60):
    c = " ".join((m.get("caption") or "(sem legenda)").split())
    return (c[:n] + "…") if len(c) > n else c


def post_summary(p):
    by_kind = {}
    for m in p.posts:
        by_kind.setdefault(media_kind(m), []).append(m["insights"].get("reach") or 0)
    return {k: (len(v), statistics.median(v)) for k, v in by_kind.items()}


def render(account, base, cur, demo):
    L = []
    w = L.append
    w(f"# Relatório do Instagram: @{account['username']}")
    w("")
    w(f"Gerado em {datetime.now(TZ):%d/%m/%Y %H:%M}. Seguidores hoje: **{fmt(account.get('followers_count'))}**.")
    w("")
    w(f"- **Base:** {base.label}")
    w(f"- **Atual:** {cur.label}")
    if base.days != cur.days:
        w(f"- Os períodos têm tamanhos diferentes; a coluna \"por dia\" normaliza a comparação.")
    w("")

    w("## Visão geral")
    w("")
    w("| Métrica | Base | Atual | Variação | Base/dia | Atual/dia |")
    w("|---|---:|---:|---:|---:|---:|")
    nf_b = sum(v for _, v in base.new_followers)
    nf_c = sum(v for _, v in cur.new_followers)
    rows = [("follower_count", "Novos seguidores", nf_b, nf_c)]
    rows += [(m, lbl, base.totals.get(m), cur.totals.get(m)) for m, lbl in PERIOD_METRICS]
    rows += [("posts", "Publicações no período", len(base.posts), len(cur.posts))]
    for key, lbl, b, c in rows:
        per_b = b / base.days if b is not None else None
        per_c = c / cur.days if c is not None else None
        w(f"| {lbl} | {fmt(b)} | {fmt(c)} | {delta(per_b, per_c)} | {fmt(per_b, 1)} | {fmt(per_c, 1)} |")
    w("")
    if base.reach_is_sum or cur.reach_is_sum:
        w("> Em períodos com mais de 30 dias, o alcance é a soma de janelas de 30 dias e pode contar a mesma pessoa mais de uma vez.")
        w("")
    w("A variação compara as médias por dia. Na API, \"Novos seguidores\" conta só quem começou a seguir, sem descontar quem deixou de seguir.")
    w("")

    w("## Publicações")
    w("")
    w("| Tipo | Base: posts | Base: alcance mediano | Atual: posts | Atual: alcance mediano |")
    w("|---|---:|---:|---:|---:|")
    sb, sc = post_summary(base), post_summary(cur)
    for k in ("REELS", "FEED", "STORY"):
        if k in sb or k in sc:
            nb, mb = sb.get(k, (0, None))
            nc, mc = sc.get(k, (0, None))
            w(f"| {KIND_LABEL[k]} | {nb} | {fmt(mb)} | {nc} | {fmt(mc)} |")
    w("")
    w("As métricas de cada post são acumuladas até hoje, não só dentro do período.")
    w("")

    for p in (base, cur):
        w(f"### Top 5 por alcance: {p.name.lower()}")
        w("")
        if not p.posts:
            w("Nenhuma publicação no período.")
            w("")
            continue
        w("| Data | Tipo | Alcance | Views | Compart. | Salvos | Seguidores | Visitas ao perfil | Post |")
        w("|---|---|---:|---:|---:|---:|---:|---:|---|")
        top = sorted(p.posts, key=lambda m: -(m["insights"].get("reach") or 0))[:5]
        for m in top:
            i = m["insights"]
            w(f"| {m['date']:%d/%m} | {KIND_LABEL[media_kind(m)]} | {fmt(i.get('reach'))} | "
              f"{fmt(i.get('views'))} | {fmt(i.get('shares'))} | {fmt(i.get('saved'))} | "
              f"{fmt(i.get('follows'))} | {fmt(i.get('profile_visits'))} | "
              f"[{caption(m)}]({m.get('permalink', '')}) |")
        w("")
    w("Em Reels, a API não informa seguidores nem visitas ao perfil por post (aparece \"–\").")
    w("")

    w("## Novos seguidores por dia")
    w("")
    for p in (base, cur):
        best = sorted(p.new_followers, key=lambda r: -r[1])[:5]
        tops = ", ".join(f"{d:%d/%m} (+{v})" for d, v in best if v) or "nenhum"
        w(f"- **{p.name}:** {fmt(sum(v for _, v in p.new_followers))} no total. Dias de pico: {tops}.")
    w("")

    if demo:
        w("## Quem segue hoje")
        w("")
        total = account.get("followers_count") or 0
        names = {"age": "Idade", "gender": "Gênero", "city": "Cidade"}
        gender = {"F": "Mulheres", "M": "Homens", "U": "Não informado"}
        for bd, rows in demo.items():
            tot = sum(v for _, v in rows) or total
            parts = [f"{gender.get(k, k) if bd == 'gender' else k} {fmt(100 * v / tot)}%"
                     for k, v in rows[:6]]
            w(f"- **{names[bd]}:** " + "; ".join(parts))
        w("")
    return "\n".join(L) + "\n"


# --- main -------------------------------------------------------------------

def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=DEFAULT_BASE_MONTH, help="mês base AAAA-MM (padrão: agosto)")
    ap.add_argument("--base-start", type=parse_date, help="início do período base (AAAA-MM-DD)")
    ap.add_argument("--base-end", type=parse_date, help="fim do período base, inclusivo")
    ap.add_argument("--current-start", type=parse_date, help="início do período atual")
    ap.add_argument("--current-end", type=parse_date, help="fim do período atual (padrão: ontem)")
    ap.add_argument("--no-demographics", action="store_true", help="não incluir demografia")
    ap.add_argument("--out", type=Path, help="arquivo de saída (padrão: reports/relatorio-AAAA-MM-DD.md)")
    args = ap.parse_args()

    b_start, b_end = month_range(args.base)
    b_start, b_end = args.base_start or b_start, args.base_end or b_end
    today = datetime.now(TZ).date()
    c_end = args.current_end or today - timedelta(days=1)
    c_start = args.current_start or c_end - timedelta(days=DEFAULT_CURRENT_DAYS - 1)
    if b_start > b_end or c_start > c_end:
        sys.exit("Período inválido: o início vem depois do fim.")

    env = load_env()
    if not env.get("IG_ACCESS_TOKEN"):
        sys.exit("IG_ACCESS_TOKEN vazio no .env.")
    client = Client(env["IG_ACCESS_TOKEN"], env.get("IG_API_VERSION", ""))

    try:
        account = client.get("me", fields="user_id,username,followers_count,media_count")
        ig_id = account.get("user_id") or account["id"]
        base = Period("Base", b_start, b_end)
        cur = Period("Atual", c_start, c_end)
        for p in (base, cur):
            print(f"Puxando {p.name.lower()}: {p.label}…")
            fetch_totals(client, ig_id, p)
            fetch_new_followers(client, ig_id, p)
        print("Puxando publicações…")
        assign_posts(client, (base, cur), fetch_media(client))
        demo = {} if args.no_demographics else fetch_demographics(client, ig_id)
    except ApiError as e:
        sys.exit(f"Erro da API: {e}")

    out = args.out or REPORTS_DIR / f"relatorio-{today.isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(account, base, cur, demo))
    print(f"Relatório salvo em {out}")


if __name__ == "__main__":
    main()
