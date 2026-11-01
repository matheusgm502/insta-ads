#!/usr/bin/env python3
"""Estética medida: paleta, brilho, contraste, saturação, calor e ritmo, calculados nos
frames dos vídeos (sem IA). O relatório resume "como o perfil parece hoje", para o usuário
só confirmar ou corrigir, em vez de descrever do zero.

    .venv/bin/python -m app.aesthetics                 # mede o que falta e gera reports/estetica.md
    .venv/bin/python -m app.aesthetics --remeasure     # recalcula as medidas de todos os frames

Exige Pillow e numpy (ambiente .venv). As medidas ficam em visual_frames. O que o modelo de
visão descreveu (cenário, enquadramento, luz) entra no relatório como contagem de palavras e
vem marcado como descrição automática, que pode errar.
"""

import argparse
import colorsys
import io
import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from app import db as appdb

ROOT = appdb.ROOT
PALETTE_SIZE = 5
SAMPLE_PIXELS = 4000
MERGE_DISTANCE = 28  # cores de paleta mais próximas que isso (RGB) viram uma só


# --- medidas de uma imagem --------------------------------------------------

def color_name(rgb):
    """Nome da cor em português, por regras de matiz, saturação e valor."""
    r, g, b = (c / 255 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    hue = h * 360
    if v < 0.18:
        return "preto"
    if s < 0.12:
        return "branco" if v > 0.85 else "cinza claro" if v > 0.6 else "cinza" if v > 0.35 else "cinza escuro"
    if 10 <= hue < 50 and s < 0.6:
        return "marrom" if v < 0.6 else "bege"
    if 10 <= hue < 40:
        return "marrom" if v < 0.6 else "laranja"
    if 40 <= hue < 70 and v < 0.45:
        return "marrom"
    if hue < 10 or hue >= 345:
        return "rosa" if (s < 0.5 and v > 0.7) else "vermelho"
    for limit, name in ((70, "amarelo"), (165, "verde"), (200, "turquesa"), (260, "azul"), (300, "roxo"), (345, "rosa")):
        if hue < limit:
            return name
    return "vermelho"


def _kmeans(pixels, k=PALETTE_SIZE, iterations=12):
    rng = np.random.default_rng(0)
    centers = pixels[rng.choice(len(pixels), size=min(k, len(pixels)), replace=False)].astype(np.float32)
    for _ in range(iterations):
        labels = np.argmin(((pixels[:, None, :] - centers[None]) ** 2).sum(axis=2), axis=1)
        for i in range(len(centers)):
            members = pixels[labels == i]
            if len(members):
                centers[i] = members.mean(axis=0)
    labels = np.argmin(((pixels[:, None, :] - centers[None]) ** 2).sum(axis=2), axis=1)
    return centers, np.bincount(labels, minlength=len(centers)) / len(pixels)


def palette(array):
    """Cores dominantes: lista de {hex, nome, share}, da maior para a menor."""
    pixels = array.reshape(-1, 3)
    if len(pixels) > SAMPLE_PIXELS:
        pixels = pixels[np.random.default_rng(0).choice(len(pixels), SAMPLE_PIXELS, replace=False)]
    centers, shares = _kmeans(pixels.astype(np.float32))
    merged = []  # junta centros quase iguais
    for center, share in sorted(zip(centers, shares), key=lambda cs: -cs[1]):
        for entry in merged:
            if np.linalg.norm(entry["rgb"] - center) < MERGE_DISTANCE:
                entry["rgb"] = (entry["rgb"] * entry["share"] + center * share) / (entry["share"] + share)
                entry["share"] += share
                break
        else:
            merged.append({"rgb": center.copy(), "share": float(share)})
    out = []
    for entry in sorted(merged, key=lambda e: -e["share"]):
        rgb = tuple(int(round(c)) for c in entry["rgb"])
        out.append({"hex": "#%02x%02x%02x" % rgb, "nome": color_name(rgb), "share": round(entry["share"], 3)})
    return out


def measure_image(image):
    """Brilho, contraste, saturação (0 a 1), calor (-1 frio a +1 quente) e paleta de uma imagem PIL."""
    image = image.convert("RGB")
    image.thumbnail((160, 160))
    array = np.asarray(image, dtype=np.float32)
    gray = array @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32) / 255
    mx, mn = array.max(axis=2), array.min(axis=2)
    saturation = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    return {
        "brightness": round(float(gray.mean()), 4),
        "contrast": round(float(gray.std()), 4),
        "saturation": round(float(saturation.mean()), 4),
        "warmth": round(float((array[..., 0].mean() - array[..., 2].mean()) / 255), 4),
        "palette": palette(array),
    }


def measure_jpeg(data):
    return measure_image(Image.open(io.BytesIO(data)))


# --- medir os frames do banco -----------------------------------------------

def measure_missing(db, remeasure=False):
    where = "" if remeasure else "WHERE brightness IS NULL"
    rows = db.execute(f"SELECT media_id, idx, path FROM visual_frames {where}").fetchall()
    done = 0
    for media_id, idx, path in rows:
        file = ROOT / path
        if not file.exists():
            continue
        m = measure_image(Image.open(file))
        db.execute("UPDATE visual_frames SET brightness=?, contrast=?, saturation=?, warmth=?, palette=? "
                   "WHERE media_id=? AND idx=?",
                   (m["brightness"], m["contrast"], m["saturation"], m["warmth"], json.dumps(m["palette"]),
                    media_id, idx))
        db.commit()
        done += 1
    return done, len(rows)


# --- perfil agregado --------------------------------------------------------

KEYWORDS = {
    "cenario": {"sala de estar": ("sala",), "escritório": ("escritório", "consultório"), "quarto": ("quarto",),
                "poltrona ou sofá": ("poltrona", "sofá"), "estante ou livros": ("estante", "livro"),
                "quadros ao fundo": ("quadro",), "ambiente externo": ("externo", "rua", "parque", "jardim")},
    "enquadramento": {"close": ("close",), "plano médio": ("médio", "medio"), "plano aberto": ("aberto", "geral")},
    "iluminacao": {"luz natural": ("natural", "janela"), "luz artificial": ("artificial", "lâmpada", "luminária"),
                   "tom quente": ("quente", "amarel"), "tom frio": ("fria", "frio")},
}


def _per_post(db, account_id):
    """Uma linha por post: medidas médias dos frames, paleta por nome de cor e descrições."""
    posts = {}
    for media_id, source, brightness, contrast, saturation, warmth, pal, desc in db.execute(
            """SELECT f.media_id, f.source, f.brightness, f.contrast, f.saturation, f.warmth, f.palette, f.description
               FROM visual_frames f JOIN media m ON m.id = f.media_id
               WHERE m.account_id = ? AND f.brightness IS NOT NULL""", (account_id,)):
        p = posts.setdefault(media_id, {"source": source, "m": [], "palettes": [], "desc": []})
        p["m"].append((brightness, contrast, saturation, warmth))
        p["palettes"].append(json.loads(pal))
        if desc:
            p["desc"].append(json.loads(desc))
    return posts


def profile(db, account_id):
    posts = _per_post(db, account_id)
    if not posts:
        return None
    rows = [tuple(statistics.mean(col) for col in zip(*p["m"])) for p in posts.values()]
    med = lambda i: statistics.median(r[i] for r in rows)
    spread = lambda i: statistics.pstdev([r[i] for r in rows])
    names = Counter()
    for p in posts.values():
        per_post = Counter()
        for pal in p["palettes"]:
            for c in pal:
                per_post[c["nome"]] += c["share"] / len(p["palettes"])
        for name, share in per_post.items():
            names[name] += share / len(posts)
    words = {}
    frames_with_desc = [d for p in posts.values() for d in p["desc"]]
    for field, groups in KEYWORDS.items():
        text = [str(d.get(field, "")).lower() for d in frames_with_desc]
        words[field] = {label: sum(any(k in t for k in keys) for t in text) / max(len(text), 1)
                        for label, keys in groups.items()}
    video = db.execute("""SELECT s.duration, s.n_cuts FROM visual_status s JOIN media m ON m.id = s.media_id
                          WHERE m.account_id = ? AND s.source = 'video'""", (account_id,)).fetchall()
    cut_rates = [c / (d / 60) for d, c in video if d and c]
    return {
        "n_posts": len(posts), "n_frames": sum(len(p["m"]) for p in posts.values()),
        "brightness": med(0), "contrast": med(1), "saturation": med(2), "warmth": med(3),
        "brightness_spread": spread(0), "warmth_spread": spread(3),
        "palette": names.most_common(6), "words": words, "frames_described": len(frames_with_desc),
        "n_videos": len(video),
        "videos_without_cuts": sum(1 for _, c in video if c == 0) / len(video) if video else None,
        "median_cuts_per_min": statistics.median(cut_rates) if cut_rates else None,
        "median_duration": statistics.median(d for d, _ in video if d) if video else None,
    }


# --- relatório --------------------------------------------------------------

def _level(value, cuts, labels):
    for limit, label in zip(cuts, labels):
        if value < limit:
            return label
    return labels[-1]


def render(p):
    pct = lambda x: f"{round(100 * x)}%"
    light = _level(p["brightness"], (0.35, 0.6), ("escura", "média", "clara"))
    heat = _level(p["warmth"], (-0.05, 0.05), ("fria", "neutra", "quente"))
    sat = _level(p["saturation"], (0.25, 0.5), ("suave (pouco saturada)", "moderada", "vívida"))
    consistent = "bem consistente" if p["brightness_spread"] < 0.1 and p["warmth_spread"] < 0.08 else "variada"
    L = ["# Estética hoje (medida nos frames dos vídeos)", "",
         f"Gerado em {datetime.now():%d/%m/%Y %H:%M}. Base: **{p['n_posts']} posts** ({p['n_frames']} frames). "
         "As medidas são cálculo sobre os pixels, sem IA; confirmar ou corrigir é com ela.", "",
         "## Resumo para confirmar", "",
         f"- **Luz e cor:** imagem {light}, de tom {heat} e {sat}. Variação entre posts: {consistent}.",
         "- **Paleta dominante:** " + ", ".join(f"{n} ({pct(s)})" for n, s in p["palette"]) + ".",
         ]
    if p["n_videos"]:
        cuts = (f"{pct(p['videos_without_cuts'])} dos vídeos são uma tomada contínua (sem corte)"
                + (f"; nos demais, ~{p['median_cuts_per_min']:.0f} cortes por minuto" if p["median_cuts_per_min"] else ""))
        L.append(f"- **Ritmo:** {cuts}. Duração mediana: {p['median_duration']:.0f} s.")
    L += ["", "## Medidas", "", "| Medida | Mediana | O que significa |", "|---|---:|---|",
          f"| Brilho | {p['brightness']:.2f} | 0 = escuro, 1 = claro |",
          f"| Contraste | {p['contrast']:.2f} | variação entre claro e escuro na imagem |",
          f"| Saturação | {p['saturation']:.2f} | 0 = cores apagadas, 1 = vívidas |",
          f"| Calor | {p['warmth']:+.2f} | positivo = tons quentes, negativo = frios |", ""]
    if p["frames_described"]:
        L += [f"## O que a descrição automática viu ({p['frames_described']} frames)", "",
              "Contagem de palavras nas descrições de um modelo pequeno, que **pode errar** (já confundiu cor de cabelo "
              "e de móveis entre frames do mesmo vídeo). Use como pista, não como fato.", ""]
        titles = {"cenario": "Ambiente", "enquadramento": "Enquadramento", "iluminacao": "Iluminação"}
        for field, label in titles.items():
            found = sorted(((s, n) for n, s in p["words"][field].items() if s >= 0.15), reverse=True)
            if found:
                L.append(f"- **{label}:** " + ", ".join(f"{n} ({pct(s)} dos frames)" for s, n in found))
        L.append("")
    L += ["## Como isso responde às perguntas de estética", "",
          "| Pergunta | Já medido | Falta ela |", "|---|---|---|",
          "| Ambiente | pistas da descrição automática (acima) | o que pode ou não aparecer ao fundo |",
          "| Luz | brilho, calor e contraste (acima) | preferência (natural, sem luz fria...) |",
          "| Enquadramento | pistas da descrição automática | preferência |",
          "| Cores | paleta dominante (acima) | cores da marca em código, se houver; o que evitar |",
          "| Edição e ritmo | cortes por minuto e duração | música, B-roll, velocidade |",
          "| Texto na tela | todos os vídeos têm legenda sobreposta | estilo (fonte, cor, posição) |",
          "| Referências, figurino, o que nunca mostrar | não mensurável | tudo |", ""]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--remeasure", action="store_true", help="recalcula as medidas de todos os frames")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "estetica.md")
    args = ap.parse_args()
    db = appdb.connect()
    account_id = appdb.default_account_id(db)
    done, total = measure_missing(db, remeasure=args.remeasure)
    print(f"Frames medidos agora: {done} de {total} pendentes.")
    result = profile(db, account_id)
    if not result:
        raise SystemExit("Nenhum frame medido. Rode a descrição dos vídeos (collector/describe.py) antes.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(result))
    print(f"Relatório em {args.out}")


if __name__ == "__main__":
    main()
