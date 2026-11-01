"""Medidas de estética em imagens sintéticas e migração das colunas novas."""

import io
import json
import sqlite3

import numpy as np
import pytest
from PIL import Image

from app import aesthetics, db as appdb


def solid(rgb, size=(60, 40)):
    return Image.new("RGB", size, rgb)


def test_brightness_saturation_and_warmth_of_solid_images():
    white, black = aesthetics.measure_image(solid((255, 255, 255))), aesthetics.measure_image(solid((0, 0, 0)))
    assert white["brightness"] > 0.99 and black["brightness"] < 0.01
    assert white["saturation"] == 0 and white["warmth"] == 0
    orange, blue = aesthetics.measure_image(solid((230, 130, 40))), aesthetics.measure_image(solid((40, 90, 230)))
    assert orange["warmth"] > 0.5 and blue["warmth"] < -0.5
    assert orange["saturation"] > 0.7


def test_contrast_is_zero_for_a_flat_image_and_high_for_a_half_black_half_white_one():
    flat = aesthetics.measure_image(solid((120, 120, 120)))
    half = Image.new("RGB", (60, 40), (0, 0, 0))
    half.paste((255, 255, 255), (30, 0, 60, 40))
    assert flat["contrast"] < 0.01 and aesthetics.measure_image(half)["contrast"] > 0.4


def test_palette_finds_both_colors_with_their_shares_and_merges_near_duplicates():
    img = Image.new("RGB", (100, 100), (200, 40, 40))
    img.paste((30, 60, 200), (0, 0, 100, 30))  # 30% azul, 70% vermelho
    pal = aesthetics.measure_image(img)["palette"]
    assert [c["nome"] for c in pal[:2]] == ["vermelho", "azul"]
    assert pal[0]["share"] == pytest.approx(0.7, abs=0.05) and pal[1]["share"] == pytest.approx(0.3, abs=0.05)
    assert len(aesthetics.measure_image(solid((90, 90, 90)))["palette"]) == 1  # cor única não se divide em 5


def test_palette_is_deterministic():
    rng = np.random.default_rng(1)
    noisy = Image.fromarray(rng.integers(0, 255, (80, 80, 3), dtype=np.uint8))
    assert aesthetics.measure_image(noisy)["palette"] == aesthetics.measure_image(noisy)["palette"]


@pytest.mark.parametrize("rgb,name", [
    ((0, 0, 0), "preto"), ((255, 255, 255), "branco"), ((128, 128, 128), "cinza"),
    ((120, 80, 50), "marrom"), ((225, 205, 175), "bege"), ((220, 30, 30), "vermelho"),
    ((40, 90, 220), "azul"), ((40, 170, 70), "verde"), ((240, 200, 40), "amarelo"),
    ((240, 150, 190), "rosa"), ((140, 60, 200), "roxo"),
])
def test_color_names_in_portuguese(rgb, name):
    assert aesthetics.color_name(rgb) == name


def test_measure_jpeg_accepts_encoded_bytes():
    buf = io.BytesIO()
    solid((200, 150, 100)).save(buf, "JPEG")
    assert aesthetics.measure_jpeg(buf.getvalue())["warmth"] > 0.2


def test_v1_database_upgrades_to_v2_keeping_frames_and_adding_empty_metrics(tmp_path, monkeypatch):
    path = tmp_path / "x.sqlite"
    monkeypatch.setattr(appdb, "MIGRATIONS", appdb.MIGRATIONS[:1])
    monkeypatch.setattr(appdb, "LATEST", 1)
    db = appdb.connect(path)
    acc = appdb.ensure_account(db, "a")
    db.execute("INSERT INTO media (id, account_id) VALUES ('m', ?)", (acc,))
    db.execute("INSERT INTO visual_frames (media_id, idx, t) VALUES ('m', 0, 0.5)")
    db.commit()
    db.close()
    monkeypatch.undo()
    db = appdb.connect(path)
    assert appdb.version(db) == 2
    assert db.execute("SELECT t, brightness, palette FROM visual_frames").fetchone() == (0.5, None, None)
    assert len(list((tmp_path / "backups").glob("*.sqlite"))) == 1


def test_profile_and_report_from_measured_frames(tmp_path):
    db = appdb.connect(tmp_path / "x.sqlite")
    acc = appdb.ensure_account(db, "a")
    for i, rgb in enumerate([(200, 150, 100), (210, 160, 110), (190, 140, 90)]):
        db.execute("INSERT INTO media (id, account_id) VALUES (?, ?)", (f"m{i}", acc))
        m = aesthetics.measure_image(solid(rgb))
        db.execute("INSERT INTO visual_status (media_id, source, duration, n_frames, n_cuts) VALUES (?, 'video', 30, 1, ?)",
                   (f"m{i}", 0 if i else 2))
        db.execute("INSERT INTO visual_frames (media_id, idx, t, source, brightness, contrast, saturation, warmth, palette, "
                   "description) VALUES (?, 0, 0.5, 'video', ?, ?, ?, ?, ?, ?)",
                   (f"m{i}", m["brightness"], m["contrast"], m["saturation"], m["warmth"], json.dumps(m["palette"]),
                    json.dumps({"cenario": "sala de estar com poltrona", "enquadramento": "close", "iluminacao": "luz natural"})))
    db.commit()
    p = aesthetics.profile(db, acc)
    assert p["n_posts"] == 3 and p["warmth"] > 0.1
    assert p["videos_without_cuts"] == pytest.approx(2 / 3)
    text = aesthetics.render(p)
    assert "Estética hoje" in text and "bege" in text and "pode errar" in text
    assert "sala de estar" in text and "close" in text


def test_measure_missing_fills_only_frames_without_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(aesthetics, "ROOT", tmp_path)
    db = appdb.connect(tmp_path / "x.sqlite")
    acc = appdb.ensure_account(db, "a")
    db.execute("INSERT INTO media (id, account_id) VALUES ('m', ?)", (acc,))
    solid((100, 100, 100)).save(tmp_path / "f.jpg")
    db.execute("INSERT INTO visual_frames (media_id, idx, path) VALUES ('m', 0, 'f.jpg')")
    db.execute("INSERT INTO visual_frames (media_id, idx, path, brightness) VALUES ('m', 1, 'f.jpg', 0.99)")
    db.commit()
    assert aesthetics.measure_missing(db) == (1, 1)
    assert db.execute("SELECT brightness FROM visual_frames WHERE idx = 1").fetchone()[0] == 0.99  # não foi sobrescrito
    assert db.execute("SELECT brightness FROM visual_frames WHERE idx = 0").fetchone()[0] == pytest.approx(0.39, abs=0.02)
