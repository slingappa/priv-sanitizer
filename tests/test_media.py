from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from types import SimpleNamespace
from unittest.mock import patch
import sys

from priv_sanitizer.media import (
    _blur_filter_graph,
    _sample_entries,
    detect_face_regions,
    detect_layout_regions,
    detect_ocr_regions,
    sample_frames,
    _verbose,
)


FRAME = Path(__file__)
LAYOUT = [{"kind": "participant_panel", "box": [0.75, 0.0, 0.25, 1.0], "start": 0.0, "end": 480.0}]


def test_blur_graph_is_regional_not_black_fill():
    graph, output = _blur_filter_graph(LAYOUT)
    assert "boxblur" in graph
    assert "drawbox" not in graph
    assert output == "[vout]"


def test_ocr_backend_contract():
    class FakeImage:
        size = (1920, 1080)
        width = 1920
        height = 1080

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def convert(self, mode):
            return self

        def crop(self, box):
            return self

    fake = SimpleNamespace(__version__="test", Output=SimpleNamespace(DICT="dict"))
    fake.image_to_data = lambda image, output_type=None, config=None: {
        "text": ["Person Alpha"], "conf": ["95"], "left": ["20"],
        "top": ["800"], "width": ["180"], "height": ["30"],
    }
    with patch.dict(sys.modules, {"pytesseract": fake}), patch(
        "shutil.which", side_effect=lambda name: "/fake/tesseract" if name == "tesseract" else None
    ), patch("PIL.Image.open", return_value=FakeImage()):
        regions, evidence, _ = detect_ocr_regions([FRAME], LAYOUT, ["Person Alpha"])
    assert evidence["available"] is True
    assert regions and regions[0]["kind"] == "participant_name"


def test_face_backend_contract():
    class FakeImage:
        shape = (1080, 1920, 3)

        def __getitem__(self, key):
            return self

    class FakeCascade:
        def empty(self):
            return False

        def detectMultiScale(self, roi, **kwargs):
            return [(100, 100, 50, 60)]

    class FakeCV2:
        __version__ = "test"
        COLOR_BGR2GRAY = 0
        data = SimpleNamespace(haarcascades="/tmp")

        @staticmethod
        def imread(path):
            return FakeImage()

        @staticmethod
        def cvtColor(image, code):
            return image

        @staticmethod
        def CascadeClassifier(path):
            return FakeCascade()

    with patch.dict(sys.modules, {"cv2": FakeCV2()}):
        regions, evidence, _ = detect_face_regions([FRAME], LAYOUT, 480.0)
    assert evidence["available"] is True
    assert regions and regions[0]["kind"] == "participant_face"


def test_face_backend_handles_arbitrary_grid_tile():
    class FakeImage:
        shape = (1080, 1920, 3)

        def __getitem__(self, key):
            return self

    class FakeCascade:
        def empty(self):
            return False

        def detectMultiScale(self, roi, **kwargs):
            return [(500, 300, 50, 60)]

    class FakeCV2:
        __version__ = "test"
        COLOR_BGR2GRAY = 0
        data = SimpleNamespace(haarcascades="/tmp")

        @staticmethod
        def imread(path):
            return FakeImage()

        @staticmethod
        def cvtColor(image, code):
            return image

        @staticmethod
        def CascadeClassifier(path):
            return FakeCascade()

    with patch.dict(sys.modules, {"cv2": FakeCV2()}):
        regions, evidence, _ = detect_face_regions([FRAME], [], 480.0)
    assert evidence["available"] is True
    assert regions and regions[0]["kind"] == "participant_tile"
    assert regions[0]["box"] != [0.75, 0.0, 0.25, 1.0]


def test_sample_entries_use_sidecar_and_preserve_source_zero(tmp_path):
    first = tmp_path / "frame-000000.jpg"
    second = tmp_path / "frame-000001.jpg"
    first.touch()
    second.touch()
    (tmp_path / "frame-timestamps.json").write_text(
        '{"frame-000000.jpg": 0.0, "frame-000001.jpg": 4.0}\n', encoding="utf-8"
    )
    entries = _sample_entries([first, second], 8.0)
    assert entries[0][1:] == (0.0, 0.0, 2.0)
    assert entries[1][1:] == (4.0, 2.0, 8.0)


def test_sample_frames_drops_duplicate_periodic_source_zero(tmp_path):
    def fake_run(args, **kwargs):
        output = Path(args[-1])
        if "select=eq(n\\,0)" in args:
            output.write_bytes(b"first")
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            # fps=1/4 emits source-time-zero and the t=4 frame.  The first
            # periodic image must be discarded because frame zero is already
            # decoded explicitly by sample_frames().
            for index in (1, 2):
                output.parent.joinpath(f"periodic-{index:06d}.jpg").write_bytes(b"periodic")

    with patch("priv_sanitizer.media.run", side_effect=fake_run):
        frames = sample_frames(Path("input.mp4"), tmp_path, every=4.0, duration=8.0)

    assert [path.name for path in frames] == ["frame-000000.jpg", "frame-000001.jpg"]
    timestamps = __import__("json").loads((tmp_path / "frame-timestamps.json").read_text())
    assert timestamps == {"frame-000000.jpg": 0.0, "frame-000001.jpg": 4.0}


def test_verbose_details_are_log_only(tmp_path):
    log = tmp_path / "verbose.log"
    stdout = StringIO()
    stderr = StringIO()
    with patch.dict(os.environ, {
        "PRIV_SANITIZER_VERBOSE": "1",
        "PRIV_SANITIZER_QUIET": "0",
        "PRIV_SANITIZER_VERBOSE_LOG": str(log),
    }), redirect_stdout(stdout), redirect_stderr(stderr):
        _verbose("command details")
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""
    assert "verbose: command details" in log.read_text(encoding="utf-8")


def test_layout_detection_is_temporal_not_global(tmp_path):
    from PIL import Image

    first = tmp_path / "frame-000000.jpg"
    second = tmp_path / "frame-000001.jpg"
    Image.new("RGB", (100, 100), (20, 20, 20)).save(first)
    image = Image.new("RGB", (100, 100), (220, 220, 220))
    for x in range(75, 100):
        for y in range(100):
            image.putpixel((x, y), (10, 10, 10))
    image.save(second)
    (tmp_path / "frame-timestamps.json").write_text(
        '{"frame-000000.jpg": 0.0, "frame-000001.jpg": 4.0}\n', encoding="utf-8"
    )
    regions, _, evidence = detect_layout_regions([first, second], 8.0, 100, 100)
    assert evidence["temporal_region_count"] == 1
    assert regions[0]["start"] == 2.0
    assert regions[0]["end"] == 8.0
