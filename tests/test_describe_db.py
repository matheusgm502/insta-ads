"""describe.py grava frames e status com as chaves estrangeiras impostas. Sem Ollama e sem rede."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import describe  # noqa: E402
import ig_collect  # noqa: E402
from app import db as appdb  # noqa: E402

MEDIA = {"id": "v1", "media_type": "VIDEO", "media_product_type": "REELS",
         "timestamp": "2026-10-01T10:00:00+0000", "permalink": "l", "caption": "c", "media_url": "http://x"}
DESCRIPTION = {"cenario": "sala", "iluminacao": "natural", "enquadramento": "close", "pessoas": 1,
               "pessoa_descricao": "", "acao": "", "texto_na_tela": "", "elementos_graficos": "",
               "cores_predominantes": ""}


class FakeClient:
    def __init__(self, *a, **k):
        pass

    def paged(self, path, **k):
        return [dict(MEDIA)]


def run_describe(monkeypatch, tmp_path, *args):
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(ig_collect, "DB_PATH", tmp_path / "x.sqlite")
    monkeypatch.setattr(describe, "ROOT", tmp_path)
    monkeypatch.setattr(describe, "FRAMES_DIR", tmp_path / "data" / "frames")
    monkeypatch.setattr(describe, "load_env", lambda: {"IG_ACCESS_TOKEN": "t"})
    monkeypatch.setattr(describe, "Client", FakeClient)
    monkeypatch.setattr(describe, "start_ollama", lambda: None)
    monkeypatch.setattr(describe, "ensure_model", lambda: None)
    monkeypatch.setattr(describe, "describe_image", lambda jpeg: dict(DESCRIPTION))
    monkeypatch.setattr(describe, "frames_for",
                        lambda m, tmp: ("video", 10.0, [(0.5, False, b"jpg"), (5.0, True, b"jpg")]))
    monkeypatch.setattr(sys, "argv", ["describe.py", *args])
    describe.main()


def test_describe_writes_frames_and_status_with_enforcement_on(monkeypatch, tmp_path):
    db = appdb.connect(tmp_path / "x.sqlite")
    appdb.ensure_account(db, "a", "conta_teste")
    db.commit()
    db.close()
    run_describe(monkeypatch, tmp_path)
    db = appdb.connect(tmp_path / "x.sqlite")
    assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM visual_frames WHERE media_id = 'v1'").fetchone()[0] == 2
    assert db.execute("SELECT n_frames, n_cuts FROM visual_status WHERE media_id = 'v1'").fetchone() == (2, 1)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()
    run_describe(monkeypatch, tmp_path, "--force")  # redescrever não perde nem duplica nada
    db = appdb.connect(tmp_path / "x.sqlite")
    assert db.execute("SELECT COUNT(*) FROM visual_frames").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM visual_status").fetchone()[0] == 1
