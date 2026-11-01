#!/usr/bin/env python3
"""Descreve o visual de cada post: extrai frames dos vídeos e descreve cada um com
um modelo de visão local (Ollama + Qwen2.5-VL 3B). Só descrição, sem avaliação.

    .venv/bin/python collector/describe.py                    # Reels/vídeos ainda não descritos
    .venv/bin/python collector/describe.py --include-images   # inclui posts de foto e carrossel
    .venv/bin/python collector/describe.py --limit 2 --force

Frames: o primeiro (~0,5 s, gancho) e um em cada corte de cena detectado, no máximo
MAX_FRAMES. Vídeo sem corte tem um único frame: não se reavalia o que não mudou. Vídeos
sem arquivo na API usam só a capa. Os frames ficam em data/frames/<id>/ e as descrições em data/instagram.sqlite
(tabelas visual_frames e visual_status). Tudo local e fora do git.
"""

import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from ig_collect import ROOT, ApiError, Client, appdb, load_env, log, now_iso, open_db

MODEL = "qwen2.5vl:3b"
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_BIN = os.environ.get("OLLAMA_BIN", str(Path.home() / ".local/ollama/bin/ollama"))
FRAMES_DIR = ROOT / "data" / "frames"

SAMPLE_EVERY = 0.5     # s entre amostras para detectar cortes
CUT_THRESHOLD = 28.0   # diferença média de pixel (0–255) que conta como corte
MIN_SPACING = 1.0      # s mínimos entre frames escolhidos
MAX_FRAMES = 8          # teto para vídeos com muitos cortes
FRAME_MAX_SIDE = 768   # px; imagens menores = descrição mais rápida

PROMPT = """Esta imagem é um quadro de um post do Instagram de um perfil profissional.
Descreva objetivamente o que se vê, em português do Brasil. Não avalie, não opine
e não sugira melhorias: apenas descreva. Se algo não estiver visível, use "".
- cenario: o local e o fundo (cômodo, objetos ao fundo, externo/interno)
- iluminacao: natural ou artificial, de onde vem, intensidade, tom (quente/frio)
- enquadramento: plano (close, médio, aberto), ângulo e posição da pessoa no quadro
- pessoas: quantas pessoas aparecem (número)
- pessoa_descricao: roupa, expressão, gesto e se olha para a câmera
- acao: o que está acontecendo neste momento
- texto_na_tela: transcrição literal de todo texto sobreposto, legenda ou título
- elementos_graficos: emojis, setas, molduras, stickers, logos
- cores_predominantes: as principais cores da imagem"""

SCHEMA = {
    "type": "object",
    "properties": {
        "cenario": {"type": "string"},
        "iluminacao": {"type": "string"},
        "enquadramento": {"type": "string"},
        "pessoas": {"type": "integer"},
        "pessoa_descricao": {"type": "string"},
        "acao": {"type": "string"},
        "texto_na_tela": {"type": "string"},
        "elementos_graficos": {"type": "string"},
        "cores_predominantes": {"type": "string"},
    },
    "required": ["cenario", "iluminacao", "enquadramento", "pessoas", "pessoa_descricao",
                 "acao", "texto_na_tela", "elementos_graficos", "cores_predominantes"],
}

# --- Ollama -----------------------------------------------------------------

def ollama_up():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError):
        return None


def start_ollama():
    """Sobe o servidor se não estiver rodando; devolve o processo (ou None)."""
    if ollama_up():
        return None
    if not Path(OLLAMA_BIN).exists():
        sys.exit(f"Ollama não encontrado em {OLLAMA_BIN} (defina OLLAMA_BIN).")
    proc = subprocess.Popen([OLLAMA_BIN, "serve"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(60):
        if ollama_up():
            return proc
        time.sleep(0.5)
    proc.terminate()
    sys.exit("O servidor do Ollama não respondeu.")


def ensure_model():
    tags = ollama_up() or {}
    if not any(m.get("name") == MODEL for m in tags.get("models", [])):
        print(f"Baixando o modelo {MODEL} (só na primeira vez)…")
        subprocess.run([OLLAMA_BIN, "pull", MODEL], check=True)


def describe_image(jpeg_bytes):
    body = json.dumps({
        "model": MODEL, "prompt": PROMPT, "stream": False, "format": SCHEMA,
        "images": [base64.b64encode(jpeg_bytes).decode()],
        "options": {"temperature": 0, "num_ctx": 4096}, "keep_alive": "10m",
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(json.load(r)["response"])


# --- frames -----------------------------------------------------------------

def to_jpeg(img):
    img = img.convert("RGB")
    img.thumbnail((FRAME_MAX_SIDE, FRAME_MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def pick_frames(video_path):
    """Devolve (duração, [(t, is_cut, jpeg)])."""
    import av
    import numpy as np

    samples = []  # (t, imagem PIL, miniatura cinza)
    with av.open(str(video_path)) as container:
        stream = container.streams.video[0]
        next_t = 0.0
        for frame in container.decode(stream):
            if frame.time is None or frame.time < next_t:
                continue
            img = frame.to_image()
            thumb = np.asarray(img.convert("L").resize((64, 36)), dtype=np.float32)
            samples.append((frame.time, img, thumb))
            next_t = frame.time + SAMPLE_EVERY
        duration = float(stream.duration * stream.time_base) if stream.duration else (
            samples[-1][0] if samples else 0.0)
    if not samples:
        return duration, []

    first = next((s for s in samples if s[0] >= 0.5), samples[0])
    chosen = {first[0]: True}  # o primeiro frame e cada corte de cena
    for prev, cur in zip(samples, samples[1:]):
        if float(np.abs(cur[2] - prev[2]).mean()) > CUT_THRESHOLD:
            if all(abs(cur[0] - t) >= MIN_SPACING for t in chosen):
                chosen[cur[0]] = True
    times = sorted(chosen)
    if len(times) > MAX_FRAMES:  # mantém o primeiro e espalha o resto
        step = (len(times) - 1) / (MAX_FRAMES - 1)
        times = [times[round(i * step)] for i in range(MAX_FRAMES)]
    by_t = {s[0]: s[1] for s in samples}
    return duration, [(t, t != first[0], to_jpeg(by_t[t])) for t in times]


def fetch_image(url):
    from PIL import Image
    with urllib.request.urlopen(url, timeout=120) as r:
        return to_jpeg(Image.open(io.BytesIO(r.read())))


def download(url, dest):
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def frames_for(media, tmp):
    """Devolve (fonte, duração, [(t, is_cut, jpeg)]) conforme o tipo do post."""
    mtype = media.get("media_type")
    if mtype == "VIDEO" and media.get("media_url"):
        path = tmp / f"{media['id']}.mp4"
        try:
            download(media["media_url"], path)
            duration, frames = pick_frames(path)
        finally:
            path.unlink(missing_ok=True)
        return "video", duration, frames
    if mtype == "VIDEO":
        url = media.get("thumbnail_url")
        return "capa", None, [(0.0, False, fetch_image(url))] if url else []
    if mtype == "CAROUSEL_ALBUM":
        out = []
        for i, child in enumerate(media.get("children", {}).get("data", [])):
            url = child.get("media_url") if child.get("media_type") == "IMAGE" else child.get("thumbnail_url")
            if url:
                out.append((float(i), False, fetch_image(url)))
        return "carrossel", None, out
    url = media.get("media_url")
    return "imagem", None, [(0.0, False, fetch_image(url))] if url else []


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--include-images", action="store_true", help="inclui posts de foto e carrossel")
    ap.add_argument("--limit", type=int, help="máximo de posts nesta execução")
    ap.add_argument("--force", action="store_true", help="redescreve posts já descritos")
    args = ap.parse_args()

    env = load_env()
    if not env.get("IG_ACCESS_TOKEN"):
        sys.exit("IG_ACCESS_TOKEN vazio no .env.")
    client = Client(env["IG_ACCESS_TOKEN"], env.get("IG_API_VERSION", ""))
    db = open_db()

    fields = ("id,caption,media_type,media_product_type,timestamp,permalink,media_url,"
              "thumbnail_url,children{media_type,media_url,thumbnail_url}")
    try:
        media = client.paged("me/media", max_items=500, fields=fields, limit=50)
    except ApiError as e:
        sys.exit(f"Erro da API: {e}")
    account_id = appdb.default_account_id(db)
    for m in media:  # garante que o post exista na tabela media
        db.execute("INSERT OR IGNORE INTO media (id, account_id, media_type, media_product_type, "
                   "timestamp, permalink, caption, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (m["id"], account_id, m.get("media_type"), m.get("media_product_type"), m.get("timestamp"),
                    m.get("permalink"), m.get("caption"), now_iso()))
    db.commit()

    done = {r[0] for r in db.execute("SELECT media_id FROM visual_status")}
    todo = [m for m in media
            if (args.include_images or m.get("media_type") == "VIDEO")
            and (args.force or m["id"] not in done)]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(media)} posts; {len(done)} já descritos; {len(todo)} a descrever agora.")
    if not todo:
        return

    proc = start_ollama()
    tmp = Path(tempfile.mkdtemp(prefix="frames-", dir=ROOT / "data"))
    try:
        ensure_model()
        for n, m in enumerate(todo, 1):
            t0 = time.time()
            try:
                source, duration, frames = frames_for(m, tmp)
            except Exception as e:
                log(db, "warn", f"frames {m['id']}: {e}")
                db.commit()
                continue
            outdir = FRAMES_DIR / m["id"]
            shutil.rmtree(outdir, ignore_errors=True)
            outdir.mkdir(parents=True)
            db.execute("DELETE FROM visual_frames WHERE media_id = ?", (m["id"],))
            db.commit()  # libera a gravação: a chamada ao modelo leva minutos
            for idx, (t, is_cut, jpeg) in enumerate(frames):
                path = outdir / f"{idx:02d}_{t:06.1f}s.jpg"
                path.write_bytes(jpeg)
                try:
                    desc = describe_image(jpeg)
                except Exception as e:
                    log(db, "warn", f"descrição {m['id']} frame {idx}: {e}")
                    desc = None
                db.execute("INSERT INTO visual_frames VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (m["id"], idx, t, source, int(is_cut), str(path.relative_to(ROOT)),
                            json.dumps(desc, ensure_ascii=False) if desc else None, MODEL, now_iso()))
                db.commit()
            n_cuts = sum(1 for _, c, _ in frames if c)
            db.execute("INSERT OR REPLACE INTO visual_status VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (m["id"], source, duration, len(frames), n_cuts, MODEL, now_iso()))
            db.commit()
            print(f"[{n}/{len(todo)}] {m['timestamp'][:10]} {source}: {len(frames)} frames, "
                  f"{n_cuts} cortes, {time.time() - t0:.0f} s")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if proc:
            proc.terminate()
        db.close()


if __name__ == "__main__":
    main()
