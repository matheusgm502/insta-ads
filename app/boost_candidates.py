#!/usr/bin/env python3
"""Candidatos a impulsionamento: os posts que melhor performaram no próprio perfil,
com uma triagem automática de risco.

A triagem procura, na legenda, na fala e no texto sobreposto dos frames analisados (o início e cada corte), sinais de quebra das regras de publicidade
profissional em saúde (preço ou promoção, promessa de resultado, depoimento,
sensacionalismo), temas que a marca decidiu evitar e da política da Meta (afirmar ou sugerir que quem vê tem uma condição
de saúde). É por palavra-chave: **ajuda a priorizar a revisão humana e não a substitui**.
Ausência de alerta não significa que o conteúdo esteja de acordo.

    python3 -m app.boost_candidates                  # Reels do último ano, top 8
    python3 -m app.boost_candidates --top 12 --days 180 --out /caminho/candidatos.md

Não usa IA. Lê só o banco local.
"""

import argparse
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

from app import analysis

I = re.IGNORECASE
CONDITIONS = (r"ansios\w*|ansiedade|depress\w*|trauma\w*|tdah|autis\w*|bipolar\w*|burnout|"
              r"transtorno\w*|p[âa]nico|compuls\w*")

# (código, descrição, expressão regular)
RULES = [
    ("preco_ou_promocao", "Regra do conselho: preço ou vantagem financeira como atrativo",
     re.compile(r"\b(gr[aá]tis|gratuit[oa]s?|desconto|promo[cç][aã]o|cupom|sorteio|brinde|valor social)\b|r\$\s?\d", I)),
    ("promessa_de_resultado", "Regra do conselho: promessa de resultado ou de cura",
     re.compile(r"\b(curar?|garanto|garantimos|garantid[oa]|resultado garantido)\b|(?<!\w)100\s?%", I)),
    ("depoimento_ou_caso", "Regra do conselho: depoimento de paciente ou exposição de caso",
     re.compile(r"\b(depoimentos?|meus pacientes|minha paciente|meu paciente|uma paciente|um paciente)\b", I)),
    ("sensacionalismo", "Regra do conselho: tom sensacionalista",
     re.compile(r"\b(chocante|urgente|ningu[ée]m te conta|segredo|n[ãa]o ignore|pare agora)\b", I)),
    ("tema_politico", "Marca e Meta: tema político (a marca evita; anúncio político exige autorização na Meta)",
     re.compile(r"\b(pol[ií]tic[oa]s?|governos?|presidente|elei[cç][aãõ][oe]s?|eleitor\w*|partido\w*|bolsonar\w*|lula|ditadura|golpe|senado)\b", I)),
    ("condicao_do_espectador", "Política da Meta: afirma ou sugere que quem vê tem uma condição de saúde",
     re.compile(rf"\bvoc[eê]\s+(tem|sofre|sente|est[aá]|vive|[ée])\b[^.?!]{{0,40}}\b({CONDITIONS})\b", I)),
]


def triage(post):
    """Alertas encontrados na legenda e na fala do post: lista de {code, label, excerpt}."""
    text = " ".join(t for t in (post.caption, post.speech, post.screen_text) if t)
    found = []
    for code, label, pattern in RULES:
        match = pattern.search(text)
        if match:
            start, end = max(0, match.start() - 25), min(len(text), match.end() + 25)
            found.append({"code": code, "label": label, "excerpt": " ".join(text[start:end].split())})
    if post.audio_protected:
        found.append({"code": "audio_protegido",
                      "label": "Meta: a API não entrega o vídeo quando há áudio licenciado ou protegido (inclui a biblioteca do "
                               "Instagram); anúncio com esse áudio pode ser silenciado ou reprovado. Confira o nome da música no Reel",
                      "excerpt": "arquivo de vídeo não fornecido pela API"})
    return found


def candidates(posts, days=365, top=8, kinds=("REELS",), now=None):
    """(ranking, fora da curva). A nota é a média geométrica de alcance e compartilhamentos,
    cada um relativo à mediana do tipo; posts fora da curva não entram no ranking."""
    analysis.compute_ratios(posts)
    now = now or datetime.now(analysis.TZ)
    pool = [p for p in posts if p.kind in kinds and not p.outlier and p.date >= now - timedelta(days=days)]
    ranked = [{"post": p, "score": math.sqrt(p.ratios["reach"] * p.ratios.get("shares", 1.0)), "flags": triage(p)}
              for p in pool]
    ranked.sort(key=lambda r: -r["score"])
    return ranked[:top], [p for p in posts if p.outlier and p.kind in kinds]


def _short(text, n=70):
    text = " ".join((text or "(sem texto)").split())
    return text[:n] + ("…" if len(text) > n else "")


def render(ranked, outliers, days):
    L = ["# Candidatos a impulsionamento", "",
         f"Gerado em {datetime.now(analysis.TZ):%d/%m/%Y %H:%M}. Posts dos últimos {days} dias, ordenados por uma nota que combina "
         "alcance e compartilhamentos em relação à mediana do tipo (1,0 = na mediana).", "",
         "> **A triagem é por palavra-chave e só prioriza a revisão.** Não substitui a leitura da própria profissional "
         "nem a conferência com o conselho, e a ausência de alerta não garante conformidade.", "",
         "| # | Data | Nota | Alcance | Compart. | Gancho | Triagem | Post |", "|---:|---|---:|---:|---:|---|---|---|"]
    for i, r in enumerate(ranked, 1):
        p = r["post"]
        status = "sem alertas" if not r["flags"] else "**revisar:** " + ", ".join(f["code"] for f in r["flags"])
        L.append(f"| {i} | {p.date:%d/%m/%Y} | {r['score']:.1f} | {p.ratios['reach']:.1f}x | {p.ratios.get('shares', 1):.1f}x | "
                 f"{_short(p.hook or p.caption, 55)} | {status} | [abrir]({p.permalink}) |")
    detailed = [r for r in ranked if r["flags"]]
    if detailed:
        L += ["", "## Alertas da triagem", ""]
        for r in detailed:
            L.append(f"**{r['post'].date:%d/%m/%Y}** ({_short(r['post'].hook or r['post'].caption, 50)})")
            for f in r["flags"]:
                L.append(f"- {f['label']}: \"…{f['excerpt']}…\"")
            L.append("")
    if outliers:
        L += ["", "## Fora da curva (não ranqueados)", "",
              f"Alcance de {analysis.OUTLIER_RATIO:g}x a mediana do tipo ou mais. Já chegaram a muita gente sem pagar; "
              "impulsionar não é prioridade.", ""]
        for p in outliers:
            flags = triage(p)
            review = f" **revisar:** {', '.join(f['code'] for f in flags)}" if flags else ""
            L.append(f"- {p.date:%d/%m/%Y}: alcance {analysis._n(p.metrics['reach'])} ({p.ratios['reach']:.0f}x). "
                     f"[{_short(p.hook or p.caption, 60)}]({p.permalink}){review}")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=365, help="janela em dias (padrão 365)")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--all-formats", action="store_true", help="inclui posts de feed")
    ap.add_argument("--db", type=Path)
    ap.add_argument("--out", type=Path, default=analysis.ROOT / "reports" / "candidatos.md")
    args = ap.parse_args()
    posts = analysis.load_posts(args.db)
    kinds = ("REELS", "FEED") if args.all_formats else ("REELS",)
    ranked, outliers = candidates(posts, days=args.days, top=args.top, kinds=kinds)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(ranked, outliers, args.days))
    review = sum(1 for r in ranked if r["flags"])
    print(f"{len(ranked)} candidatos ({review} com alertas para revisar). Relatório em {args.out}")


if __name__ == "__main__":
    main()
