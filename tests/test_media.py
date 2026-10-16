from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

from priv_sanitizer.media import _blur_filter_graph, detect_face_regions, detect_ocr_regions


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
