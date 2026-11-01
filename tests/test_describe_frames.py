"""Escolha de frames: o primeiro e um por corte de cena. Vídeos sintéticos gerados no teste."""

import io
import sys
from pathlib import Path

import av
import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import describe  # noqa: E402


def make_video(path, scenes, fps=10, size=(160, 90)):
    """Vídeo de cores sólidas: `scenes` é uma lista de (nível de cinza, segundos)."""
    container = av.open(str(path), "w")
    try:
        stream = container.add_stream("libx264", rate=fps)
    except Exception:
        stream = container.add_stream("mpeg4", rate=fps)
    stream.width, stream.height, stream.pix_fmt = size[0], size[1], "yuv420p"
    for gray, seconds in scenes:
        for _ in range(int(seconds * fps)):
            frame = av.VideoFrame.from_ndarray(np.full((size[1], size[0], 3), gray, dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()


def test_one_frame_per_scene_cut_plus_the_first(tmp_path):
    video = tmp_path / "tres_cenas.mp4"
    make_video(video, [(40, 2), (140, 2), (240, 2)])
    duration, frames = describe.pick_frames(video)
    times = [round(t, 1) for t, _, _ in frames]
    assert len(frames) == 3
    assert times[0] == pytest.approx(0.5, abs=0.2)
    assert times[1] == pytest.approx(2.0, abs=0.6) and times[2] == pytest.approx(4.0, abs=0.6)
    assert [cut for _, cut, _ in frames] == [False, True, True]
    assert duration == pytest.approx(6.0, abs=0.3)


def test_a_video_without_cuts_gets_a_single_frame_however_long_it_is(tmp_path):
    video = tmp_path / "sem_corte.mp4"
    make_video(video, [(120, 20)])  # 20 s sem mudança: não reavalia
    _, frames = describe.pick_frames(video)
    assert len(frames) == 1 and frames[0][1] is False


def test_many_cuts_are_capped_and_keep_the_first_frame(tmp_path):
    video = tmp_path / "muitos_cortes.mp4"
    make_video(video, [(20 if i % 2 == 0 else 230, 1) for i in range(16)])
    _, frames = describe.pick_frames(video)
    assert len(frames) == describe.MAX_FRAMES
    assert frames[0][1] is False and frames[0][0] == pytest.approx(0.5, abs=0.2)


def test_frames_are_returned_as_jpeg_within_the_size_limit(tmp_path):
    video = tmp_path / "v.mp4"
    make_video(video, [(100, 1)], size=(1280, 720))
    _, frames = describe.pick_frames(video)
    image = Image.open(io.BytesIO(frames[0][2]))
    assert image.format == "JPEG" and max(image.size) <= describe.FRAME_MAX_SIDE
