#!/usr/bin/env python3
"""Monta o conjunto de dados por post: metadados + métricas + fala (transcrição) +
visual (descrição dos frames), alinhados numa linha do tempo.

    python3 collector/dataset.py

Lê só o banco local (data/instagram.sqlite), sem chamar a API. Gera:
- data/dataset/posts.jsonl              um post por linha (para análise/LLM)
- data/dataset/posts/<data>_<id>.json   um arquivo por post
- reports/posts/<data>_<id>.md          versão legível, com a linha do tempo
Tudo fora do git.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ig_collect import DB_PATH, ROOT, TZ, appdb

OUT_DIR = ROOT / "data" / "dataset"
MD_DIR = ROOT / "reports" / "posts"
METRICS = ["reach", "views", "likes", "comments", "shares", "saved", "total_interactions",
           "follows", "profile_visits", "ig_reels_avg_watch_time"]


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)).fetchone() is not None


def latest_metrics(db, media_id):
    rows = db.execute("""
        SELECT metric, value FROM media_insights WHERE media_id = ? AND collected_date =
            (SELECT MAX(collected_date) FROM media_insights WHERE media_id = ?)""",
                      (media_id, media_id)).fetchall()
    found = dict(rows)
    return {m: found[m] for m in METRICS if m in found}


def audio_for(db, media_id, has_transcripts, has_skips):
    if has_transcripts:
        row = db.execute("SELECT hook, text, segments, duration, model FROM transcripts "
                         "WHERE media_id = ?", (media_id,)).fetchone()
        if row:
            hook, text, segs, duration, model = row
            return {"status": "transcrito" if text else "sem fala", "model": model,
                    "duration": duration, "hook": hook, "text": text,
                    "segments": json.loads(segs or "[]")}
    if has_skips and db.execute("SELECT 1 FROM transcript_skips WHERE media_id = ?",
                                (media_id,)).fetchone():
        return {"status": "sem arquivo na API"}
    return None


def visual_for(db, media_id, has_visual):
    if not has_visual:
        return None
    st = db.execute("SELECT source, duration, n_frames, n_cuts, model FROM visual_status "
                    "WHERE media_id = ?", (media_id,)).fetchone()
    if not st:
        return None
    frames = [{"t": t, "is_cut": bool(cut), "path": path,
               "description": json.loads(desc) if desc else None,
               "measures": {"brightness": b, "contrast": c, "saturation": s, "warmth": w,
                            "palette": json.loads(pal) if pal else None}}
              for t, cut, path, desc, b, c, s, w, pal in db.execute(
                  "SELECT t, is_cut, path, description, brightness, contrast, saturation, warmth, palette "
                  "FROM visual_frames WHERE media_id = ? ORDER BY idx", (media_id,))]
    return {"source": st[0], "duration": st[1], "n_frames": st[2], "n_cuts": st[3],
            "model": st[4], "frames": frames}


def timeline(audio, visual):
    """Junta cada frame com a fala que acontece até o próximo frame."""
    if not visual or visual["source"] != "video":
        return []
    frames = visual["frames"]
    segs = (audio or {}).get("segments") or []
    end_all = visual.get("duration") or (segs[-1]["end"] if segs else frames[-1]["t"] + 1)
    out = []
    for i, f in enumerate(frames):
        start = 0.0 if i == 0 else f["t"]
        end = frames[i + 1]["t"] if i + 1 < len(frames) else end_all
        speech = " ".join(s["text"] for s in segs if s["start"] < end and s["end"] > start)
        out.append({"start": round(start, 1), "end": round(end, 1), "frame_t": f["t"],
                    "is_cut": f["is_cut"], "visual": f["description"], "speech": speech})
    return out


def mmss(t):
    t = int(t or 0)
    return f"{t // 60:02d}:{t % 60:02d}"


def render_md(p):
    d = datetime.fromisoformat(p["date"])
    L = [f"# {d:%d/%m/%Y} · {p['type']}", "", f"[Abrir no Instagram]({p['permalink']})", ""]
    if p["caption"]:
        L += ["**Legenda:** " + " ".join(p["caption"].split()), ""]
    if p["metrics"]:
        L += ["**Métricas:** " + ", ".join(f"{k} {v:g}" for k, v in p["metrics"].items()), ""]
    a, v = p["audio"], p["visual"]
    if a:
        L += [f"**Áudio:** {a['status']}" + (f". Gancho: \"{a['hook']}\"" if a.get("hook") else ""), ""]
    if v:
        L += [f"**Visual:** {v['source']}, {v['n_frames']} frames"
              + (f", {v['n_cuts']} cortes, {mmss(v['duration'])} de duração" if v["source"] == "video" else ""), ""]
    if p["timeline"]:
        L += ["## Linha do tempo", ""]
        for step in p["timeline"]:
            vis = step["visual"] or {}
            L.append(f"### {mmss(step['start'])}–{mmss(step['end'])}" + (" · corte" if step["is_cut"] else ""))
            L.append("")
            for key, label in [("cenario", "Cenário"), ("iluminacao", "Iluminação"),
                               ("enquadramento", "Enquadramento"), ("pessoa_descricao", "Pessoa"),
                               ("acao", "Ação"), ("texto_na_tela", "Texto na tela"),
                               ("elementos_graficos", "Elementos gráficos"),
                               ("cores_predominantes", "Cores")]:
                if vis.get(key):
                    L.append(f"- **{label}:** {vis[key]}")
            if step["speech"]:
                L.append(f"- **Fala:** {step['speech']}")
            L.append("")
    elif v:
        L += ["## Imagens", ""]
        for i, f in enumerate(v["frames"], 1):
            vis = f["description"] or {}
            L.append(f"### Imagem {i}")
            L.append("")
            for key in ("cenario", "iluminacao", "enquadramento", "pessoa_descricao", "acao",
                        "texto_na_tela", "elementos_graficos", "cores_predominantes"):
                if vis.get(key):
                    L.append(f"- **{key.replace('_', ' ').capitalize()}:** {vis[key]}")
            L.append("")
    if a and a.get("text") and not p["timeline"]:
        L += ["## Fala completa", "", a["text"], ""]
    return "\n".join(L) + "\n"


def main():
    db = appdb.connect(DB_PATH)
    account_id = appdb.default_account_id(db)
    has_t = table_exists(db, "transcripts")
    has_s = table_exists(db, "transcript_skips")
    has_v = table_exists(db, "visual_status")
    posts = []
    for mid, mtype, ptype, ts, link, cap in db.execute(
            "SELECT id, media_type, media_product_type, timestamp, permalink, caption "
            "FROM media WHERE account_id = ? ORDER BY timestamp DESC", (account_id,)):
        audio = audio_for(db, mid, has_t, has_s)
        visual = visual_for(db, mid, has_v)
        local = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(TZ)
        posts.append({
            "id": mid, "date": local.isoformat(), "type": ptype or mtype, "media_type": mtype,
            "permalink": link, "caption": cap or "", "metrics": latest_metrics(db, mid),
            "audio": audio, "visual": visual, "timeline": timeline(audio, visual),
        })

    (OUT_DIR / "posts").mkdir(parents=True, exist_ok=True)
    MD_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "posts.jsonl", "w") as f:
        for p in posts:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    for p in posts:
        stem = f"{p['date'][:10]}_{p['id']}"
        (OUT_DIR / "posts" / f"{stem}.json").write_text(json.dumps(p, ensure_ascii=False, indent=2))
        (MD_DIR / f"{stem}.md").write_text(render_md(p))
    n_a = sum(1 for p in posts if p["audio"] and p["audio"].get("text"))
    n_v = sum(1 for p in posts if p["visual"])
    print(f"{len(posts)} posts: {n_a} com fala transcrita, {n_v} com visual descrito.")
    print(f"Dataset em {OUT_DIR}/posts.jsonl; versões legíveis em {MD_DIR}/")


if __name__ == "__main__":
    main()
