#!/usr/bin/env python3
"""Baixa os Reels da conta e transcreve o que foi falado (Whisper, local).

Precisa do ambiente com faster-whisper (veja .venv):

    .venv/bin/python collector/transcribe.py              # transcreve os Reels novos
    .venv/bin/python collector/transcribe.py --limit 3    # só os 3 mais recentes ainda sem transcrição
    .venv/bin/python collector/transcribe.py --model small
    .venv/bin/python collector/transcribe.py --export-only

Tudo roda na máquina: o vídeo é baixado para uma pasta temporária, transcrito e
apagado. O texto fica em data/instagram.sqlite (tabela transcripts) e o resumo
legível em reports/roteiros.md, ambos fora do git.
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from ig_collect import ROOT, TZ, ApiError, Client, appdb, load_env, log, now_iso, open_db

DEFAULT_MODEL = "medium"
# Trecho inicial tratado como "gancho" do Reel.
HOOK_SECONDS = 3.0
EXPORT_PATH = ROOT / "reports" / "roteiros.md"

def list_videos(client):
    fields = "id,caption,media_type,media_product_type,timestamp,permalink,media_url"
    items = client.paged("me/media", max_items=500, fields=fields, limit=50)
    return [m for m in items if m.get("media_type") == "VIDEO"]


def download(url, dest):
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def transcribe(model, path):
    segments, info = model.transcribe(str(path), language="pt", vad_filter=True,
                                      beam_size=5)
    segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
            for s in segments]
    text = " ".join(s["text"] for s in segs).strip()
    hook = " ".join(s["text"] for s in segs if s["start"] < HOOK_SECONDS).strip()
    if not hook and segs:
        hook = segs[0]["text"]
    return info, segs, text, hook


def export(db):
    rows = db.execute("""
        SELECT m.timestamp, m.permalink, m.caption, t.hook, t.text, t.duration,
               (SELECT value FROM media_insights i WHERE i.media_id = m.id AND i.metric = 'reach'
                ORDER BY collected_date DESC LIMIT 1),
               (SELECT value FROM media_insights i WHERE i.media_id = m.id AND i.metric = 'shares'
                ORDER BY collected_date DESC LIMIT 1)
        FROM transcripts t JOIN media m ON m.id = t.media_id
        ORDER BY m.timestamp DESC""").fetchall()
    skips = db.execute("SELECT COUNT(*) FROM transcript_skips").fetchone()[0]
    L = [f"# Roteiros dos Reels", "",
         f"Gerado em {datetime.now(TZ):%d/%m/%Y %H:%M}. {len(rows)} vídeos transcritos"
         f"{f'; {skips} sem arquivo disponível na API (em geral, música com direitos autorais)' if skips else ''}.",
         "", "Transcrição automática (Whisper): pode ter erros, sobretudo em nomes e gírias.", ""]
    for ts, link, cap, hook, text, dur, reach, shares in rows:
        d = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(TZ)
        stats = []
        if reach is not None:
            stats.append(f"alcance {int(reach):,}".replace(",", "."))
        if shares is not None:
            stats.append(f"{int(shares)} compartilhamentos")
        cap1 = " ".join((cap or "").split())[:80]
        L.append(f"## {d:%d/%m/%Y}: {cap1 or '(sem legenda)'}")
        L.append("")
        L.append(f"[Abrir no Instagram]({link}) · {round(dur or 0)} s" + (f" · {', '.join(stats)}" if stats else ""))
        L.append("")
        L.append(f"**Gancho (primeiros {HOOK_SECONDS:g} s):** {hook or '(sem fala)'}")
        L.append("")
        L.append(text or "_(sem fala detectada: provavelmente só música ou texto na tela)_")
        L.append("")
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_PATH.write_text("\n".join(L) + "\n")
    print(f"Roteiros salvos em {EXPORT_PATH}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"modelo Whisper: tiny, base, small, medium, large-v3 (padrão {DEFAULT_MODEL})")
    ap.add_argument("--limit", type=int, help="máximo de vídeos a transcrever nesta execução")
    ap.add_argument("--force", action="store_true", help="retranscreve vídeos já transcritos")
    ap.add_argument("--export-only", action="store_true", help="só regera reports/roteiros.md")
    args = ap.parse_args()

    db = open_db()
    if args.export_only:
        export(db)
        return

    env = load_env()
    if not env.get("IG_ACCESS_TOKEN"):
        sys.exit("IG_ACCESS_TOKEN vazio no .env.")
    client = Client(env["IG_ACCESS_TOKEN"], env.get("IG_API_VERSION", ""))
    try:
        videos = list_videos(client)
    except ApiError as e:
        sys.exit(f"Erro da API: {e}")

    done = {r[0] for r in db.execute("SELECT media_id FROM transcripts")}
    todo = [v for v in videos if args.force or v["id"] not in done]
    account_id = appdb.default_account_id(db)
    for v in videos:  # garante que o post exista na tabela media para o relatório
        db.execute("INSERT OR IGNORE INTO media (id, account_id, media_type, media_product_type, "
                   "timestamp, permalink, caption, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (v["id"], account_id, v.get("media_type"), v.get("media_product_type"), v.get("timestamp"),
                    v.get("permalink"), v.get("caption"), now_iso()))
    no_url = [v for v in todo if not v.get("media_url")]
    for v in no_url:
        db.execute("INSERT OR REPLACE INTO transcript_skips VALUES (?, ?, ?)",
                   (v["id"], "sem media_url na API", now_iso()))
    todo = [v for v in todo if v.get("media_url")]
    if args.limit:
        todo = todo[:args.limit]
    db.commit()
    print(f"{len(videos)} vídeos; {len(done)} já transcritos; {len(no_url)} sem arquivo na API; "
          f"{len(todo)} a transcrever agora.")

    if todo:
        from faster_whisper import WhisperModel  # import tardio: só carrega se houver trabalho
        print(f"Carregando modelo '{args.model}' (a primeira vez baixa o modelo)…")
        model = WhisperModel(args.model, device="cpu", compute_type="int8")
        tmp = Path(tempfile.mkdtemp(prefix="reels-", dir=ROOT / "data"))
        try:
            for n, v in enumerate(todo, 1):
                path = tmp / f"{v['id']}.mp4"
                t0 = time.time()
                try:
                    download(v["media_url"], path)
                    info, segs, text, hook = transcribe(model, path)
                except Exception as e:  # um vídeo ruim não derruba o lote
                    log(db, "warn", f"transcrição {v['id']}: {e}")
                    db.commit()
                    continue
                finally:
                    path.unlink(missing_ok=True)
                db.execute("INSERT OR REPLACE INTO transcripts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (v["id"], args.model, info.language, info.duration, hook, text,
                            json.dumps(segs, ensure_ascii=False), now_iso()))
                db.commit()
                print(f"[{n}/{len(todo)}] {v['timestamp'][:10]} · {info.duration:.0f} s de vídeo "
                      f"em {time.time() - t0:.0f} s · {hook[:70]!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    export(db)
    db.close()


if __name__ == "__main__":
    main()
