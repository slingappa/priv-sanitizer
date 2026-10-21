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
    blur_radius_for_strength,
    configured_blur_strength,
    configured_face_padding,
    effective_privacy_settings,
    detect_visual_regions,
    _face_tracks,
    _face_observation_groups,
    _merge_regions,
    _non_panel_intervals,
    _coalesce_render_regions,
    _localized_face_box,
    PHYSICAL_FACE_MIN_HEIGHT,
    PHYSICAL_FACE_MIN_WIDTH,
    PHYSICAL_FACE_MAX_HEIGHT,
    PHYSICAL_FACE_MAX_WIDTH,
    adaptive_sampling_interval,
    dense_face_sampling_enabled,
    detect_face_regions,
    detect_layout_regions,
    detect_ocr_regions,
    render,
    sample_dense_face_frames,
    sample_frames,
    _scene_change_frames,
    _progress_status,
    _verbose,
)


FRAME = Path(__file__)
LAYOUT = [{"kind": "participant_panel", "box": [0.75, 0.0, 0.25, 1.0], "start": 0.0, "end": 480.0}]


def test_adaptive_sampling_dense_for_short_clips(monkeypatch):
    monkeypatch.delenv("PRIV_SANITIZER_WHITEBOARD", raising=False)
    assert adaptive_sampling_interval(4.0, 11.0) == 4.0
    monkeypatch.setenv("PRIV_SANITIZER_WHITEBOARD", "1")
    assert adaptive_sampling_interval(4.0, 11.0) == 0.5
    assert adaptive_sampling_interval(4.0, 90.0) == 1.0
    assert adaptive_sampling_interval(4.0, 480.0) == 4.0
    monkeypatch.setenv("PRIV_SANITIZER_ADAPTIVE_SAMPLING", "0")
    assert adaptive_sampling_interval(4.0, 11.0) == 4.0


def test_dense_face_sampling_is_enabled_for_normal_meetings(monkeypatch):
    monkeypatch.delenv("PRIV_SANITIZER_WHITEBOARD", raising=False)
    monkeypatch.delenv("PRIV_SANITIZER_DENSE_FACE_SAMPLING", raising=False)
    assert dense_face_sampling_enabled() is True


def test_effective_privacy_settings_use_configured_strength(monkeypatch):
    monkeypatch.delenv("PRIV_SANITIZER_WHITEBOARD", raising=False)
    monkeypatch.setenv("PRIV_SANITIZER_BLUR_STRENGTH", "50%")
    monkeypatch.setenv("PRIV_SANITIZER_FACE_PADDING", "30%")
    assert effective_privacy_settings() == {
        "whiteboard": False,
        "blur_strength": 0.5,
        "blur_radius": 64,
        "blur_opacity": 1.0,
        "face_padding": None,
    }
    monkeypatch.setenv("PRIV_SANITIZER_WHITEBOARD", "1")
    assert effective_privacy_settings() == {
        "whiteboard": True,
        "blur_strength": 0.5,
        "blur_radius": 64,
        "blur_opacity": 1.0,
        "face_padding": 0.3,
    }


def test_blur_strength_maps_zero_to_no_blur_and_full_to_max_radius():
    assert blur_radius_for_strength(0.0) == 0
    assert blur_radius_for_strength(0.5) == 64
    assert blur_radius_for_strength(1.0) == 128


def test_blur_graph_is_regional_not_black_fill():
    graph, output = _blur_filter_graph(LAYOUT)
    assert "boxblur" in graph
    assert "drawbox" not in graph
    assert output == "[vout]"


def test_normal_blur_graph_applies_configured_strength(monkeypatch):
    monkeypatch.delenv("PRIV_SANITIZER_WHITEBOARD", raising=False)
    monkeypatch.setenv("PRIV_SANITIZER_BLUR_STRENGTH", "50%")
    graph, _ = _blur_filter_graph(LAYOUT)
    assert "luma_radius=min(64\\,min(w\\,h)/2-1)" in graph
    assert "colorchannelmixer" not in graph


def test_normal_blur_graph_uses_strength_radius_and_bounds_small_crops(monkeypatch):
    monkeypatch.delenv("PRIV_SANITIZER_WHITEBOARD", raising=False)
    graph, _ = _blur_filter_graph(LAYOUT, blur_strength=0.75)
    assert "luma_radius=min(96\\,min(w\\,h)/2-1)" in graph
    assert "chroma_radius=max(0\\,min(10\\,min(w\\,h)/4-1))" in graph


def test_normal_zero_strength_leaves_blur_regions_unmodified(monkeypatch):
    monkeypatch.delenv("PRIV_SANITIZER_WHITEBOARD", raising=False)
    graph, _ = _blur_filter_graph(LAYOUT, blur_strength=0.0)
    assert "boxblur" not in graph
    assert graph == "[0:v]null[vout]"


def test_normal_blur_graph_default_remains_opaque(monkeypatch):
    monkeypatch.delenv("PRIV_SANITIZER_WHITEBOARD", raising=False)
    monkeypatch.delenv("PRIV_SANITIZER_BLUR_STRENGTH", raising=False)
    graph, _ = _blur_filter_graph(LAYOUT)
    assert "colorchannelmixer" not in graph


def test_blur_graph_accepts_strength_in_whiteboard_mode(monkeypatch):
    monkeypatch.setenv("PRIV_SANITIZER_WHITEBOARD", "1")
    graph, _ = _blur_filter_graph(LAYOUT, blur_strength=0.375)
    assert "luma_radius=min(48\\,min(w\\,h)/2-1)" in graph


def test_blur_graph_strength_is_opaque_in_whiteboard_mode(monkeypatch):
    monkeypatch.setenv("PRIV_SANITIZER_WHITEBOARD", "1")
    graph, _ = _blur_filter_graph(LAYOUT, blur_strength=0.5)
    assert "luma_radius=min(64\\,min(w\\,h)/2-1)" in graph
    assert "colorchannelmixer" not in graph


def test_physical_face_graph_uses_feathered_ellipse(monkeypatch):
    monkeypatch.setenv("PRIV_SANITIZER_WHITEBOARD", "1")
    graph, _ = _blur_filter_graph([{
        "kind": "physical_face", "mode": "blur",
        "box": [0.1, 0.2, 0.3, 0.3], "start": 0.0, "end": 1.0,
    }], blur_strength=0.375)
    assert "geq=" in graph
    assert "lte(" in graph and "gte(" in graph
    assert "0.6" in graph
    assert "gblur=sigma=3" in graph
    assert "overlay=x=main_w*0.1:y=main_h*0.2" in graph


def test_whiteboard_render_graph_is_gated_by_mode(monkeypatch):
    visual = [{
        "kind": "physical_face", "mode": "blur",
        "box": [0.1, 0.2, 0.3, 0.3], "start": 0.0, "end": 1.0,
    }]
    monkeypatch.delenv("PRIV_SANITIZER_WHITEBOARD", raising=False)
    stable_graph, _ = _blur_filter_graph(visual)
    assert "geq=" not in stable_graph
    monkeypatch.setenv("PRIV_SANITIZER_WHITEBOARD", "1")
    whiteboard_graph, _ = _blur_filter_graph(visual)
    assert "geq=" in whiteboard_graph


def test_blur_strength_defaults_to_maximum(monkeypatch):
    monkeypatch.delenv("PRIV_SANITIZER_BLUR_STRENGTH", raising=False)
    monkeypatch.delenv("PRIV_SANITIZER_BLUR_OPACITY", raising=False)
    assert configured_blur_strength() == 1.0
    monkeypatch.setenv("PRIV_SANITIZER_BLUR_STRENGTH", "75%")
    assert configured_blur_strength() == 0.75


def test_face_padding_is_symmetric_and_configurable(monkeypatch):
    monkeypatch.delenv("PRIV_SANITIZER_FACE_PADDING", raising=False)
    assert configured_face_padding() == 0.16
    monkeypatch.setenv("PRIV_SANITIZER_FACE_PADDING", "24%")
    assert configured_face_padding() == 0.24
    monkeypatch.setenv("PRIV_SANITIZER_FACE_PADDING", "0.08")
    assert configured_face_padding() == 0.08


def test_render_result_reports_manifest_strength(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "rendered" / "sanitized.mp4"
    manifest = {
        "source": {"video": str(source)},
        "video": {"duration": 1.0},
        "audio_mode": "preserve",
        "audio_redactions": [],
        "visual_redactions": [{
            "kind": "participant_tile", "mode": "blur",
            "box": [0.5, 0.0, 0.5, 1.0], "start": 0.0, "end": 1.0,
        }],
        "privacy": {"blur_strength": 0.5},
    }

    def fake_run(args, **kwargs):
        Path(args[-1]).write_bytes(b"rendered")
        return __import__("subprocess").CompletedProcess(args, 0, stderr="")

    with patch("priv_sanitizer.media.run", side_effect=fake_run):
        monkeypatch.setenv("PRIV_SANITIZER_WHITEBOARD", "1")
        monkeypatch.delenv("PRIV_SANITIZER_BLUR_STRENGTH", raising=False)
        result = render(source, manifest, output)
    assert result["blur_strength"] == 0.5
    assert result["blur_radius"] == 64
    assert result["blur_opacity"] == 1.0
    assert output.read_bytes() == b"rendered"


def test_normal_render_keeps_legacy_manifest_controls(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "rendered" / "sanitized.mp4"
    manifest = {
        "source": {"video": str(source)},
        "video": {"duration": 1.0},
        "audio_mode": "preserve",
        "audio_redactions": [],
        "visual_redactions": [{
            "kind": "participant_panel", "mode": "blur",
            "box": [0.5, 0.0, 0.5, 1.0], "start": 0.0, "end": 1.0,
        }],
        "privacy": {"blur_radius": 18, "blur_opacity": 0.5},
    }
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        Path(args[-1]).write_bytes(b"rendered")
        return __import__("subprocess").CompletedProcess(args, 0, stderr="")

    with patch("priv_sanitizer.media.run", side_effect=fake_run):
        monkeypatch.delenv("PRIV_SANITIZER_WHITEBOARD", raising=False)
        monkeypatch.delenv("PRIV_SANITIZER_BLUR_OPACITY", raising=False)
        result = render(source, manifest, output)
    assert result["blur_radius"] == 18
    assert result["blur_opacity"] == 0.5
    assert result["blur_strength"] == 0.140625
    assert any("colorchannelmixer=aa=0.500000" in str(arg) for arg in captured["args"])
    assert output.read_bytes() == b"rendered"


def test_unknown_layout_never_emits_full_frame_fallback(monkeypatch):
    monkeypatch.setenv("PRIV_SANITIZER_AGGRESSIVE", "1")
    regions, _, evidence = detect_visual_regions([], 12.0, 1920, 1080)
    assert evidence["aggressive_fallback_regions"] == 0
    assert regions == []


def test_dark_fullscreen_presenter_path_remains_available(tmp_path):
    from PIL import Image

    frame = tmp_path / "frame-000000.jpg"
    Image.new("RGB", (100, 100), (20, 20, 20)).save(frame)
    (tmp_path / "frame-timestamps.json").write_text(
        '{"frame-000000.jpg": 0.0}\n', encoding="utf-8"
    )
    regions, _, evidence = detect_visual_regions([frame], 2.0, 100, 100)
    assert evidence["dark_fullscreen_regions"] == 1
    assert any(
        item["kind"] == "participant_fullscreen"
        and item["box"] == [0.0, 0.0, 1.0, 1.0]
        for item in regions
    )


def test_bright_unknown_scene_does_not_use_fullscreen_fallback(tmp_path):
    from PIL import Image

    frame = tmp_path / "frame-000000.jpg"
    Image.new("RGB", (100, 100), (220, 220, 220)).save(frame)
    (tmp_path / "frame-timestamps.json").write_text(
        '{"frame-000000.jpg": 0.0}\n', encoding="utf-8"
    )
    regions, _, evidence = detect_visual_regions([frame], 2.0, 100, 100)
    assert evidence["dark_fullscreen_regions"] == 0
    assert not any(item["box"] == [0.0, 0.0, 1.0, 1.0] for item in regions)


def test_large_fullscreen_face_proposal_stays_local():
    box = _localized_face_box((0.0, 0.0, 0.8, 0.8))
    assert box != (0.0, 0.0, 1.0, 1.0)
    assert box[2] <= 0.85
    assert box[3] <= 0.85


def test_localized_face_box_preserves_frame_edge():
    box = _localized_face_box(
        (0.0, 0.25, 0.55, 0.20),
        pad_x_factor=0.50,
        pad_y_factor=0.50,
        max_width=0.34,
        max_height=0.38,
    )
    assert box[0] == 0.0
    assert box[2] == 0.34


def test_physical_face_safety_envelope_matches_previous_wide_mask():
    """The feathered mask must not clip an edge-moving presenter's face."""
    box = _localized_face_box(
        (0.0, 0.25, 0.40, 0.40),
        pad_x_factor=0.50,
        pad_y_factor=0.50,
        max_width=PHYSICAL_FACE_MAX_WIDTH,
        max_height=PHYSICAL_FACE_MAX_HEIGHT,
    )
    assert box[0] == 0.0
    assert box[2] == PHYSICAL_FACE_MAX_WIDTH
    assert box[3] == PHYSICAL_FACE_MAX_HEIGHT

    small = _localized_face_box(
        (0.20, 0.30, 0.06, 0.08),
        min_width=PHYSICAL_FACE_MIN_WIDTH,
        min_height=PHYSICAL_FACE_MIN_HEIGHT,
        max_width=PHYSICAL_FACE_MAX_WIDTH,
        max_height=PHYSICAL_FACE_MAX_HEIGHT,
    )
    assert small[2] == PHYSICAL_FACE_MIN_WIDTH
    assert small[3] == PHYSICAL_FACE_MIN_HEIGHT


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


def test_face_backend_uses_dense_frames_for_layout_independent_scan(tmp_path):
    class FakeImage:
        shape = (1080, 1920, 3)

        def __getitem__(self, key):
            return self

    class FakeCascade:
        def empty(self):
            return False

        def detectMultiScale(self, roi, **kwargs):
            return [(500, 300, 90, 100)]

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

    dense = tmp_path / "dense"
    dense.mkdir()
    frame = dense / "frame-000000.jpg"
    frame.touch()
    (dense / "frame-timestamps.json").write_text(
        '{"frame-000000.jpg": 7.5}\n', encoding="utf-8"
    )
    with patch.dict(
        os.environ, {"PRIV_SANITIZER_WHITEBOARD": "1"}
    ), patch.dict(sys.modules, {"cv2": FakeCV2()}):
        regions, evidence, _ = detect_face_regions(
            [FRAME], [], 20.0, face_frame_paths=[frame]
        )
    assert evidence["sampling"]["source"] == "dense-face-frames"
    assert evidence["sampling"]["frame_count"] == 1
    assert regions and regions[0]["kind"] == "participant_tile"


def test_face_backend_keeps_fullscreen_safety_at_panel_boundary(tmp_path):
    class FakeCascade:
        def empty(self):
            return False

        def detectMultiScale(self, roi, **kwargs):
            return [(20, 20, 36, 40)]

    class FakeCV2:
        __version__ = "test"
        COLOR_BGR2GRAY = 0
        data = SimpleNamespace(haarcascades="/tmp")

        @staticmethod
        def imread(path):
            import numpy as np
            return np.zeros((100, 100, 3), dtype=np.uint8)

        @staticmethod
        def cvtColor(image, code):
            import numpy as np
            return np.zeros(image.shape[:2], dtype=np.uint8)

        @staticmethod
        def CascadeClassifier(path):
            return FakeCascade()

        @staticmethod
        def flip(image, axis):
            return image

    dense = tmp_path / "dense"
    dense.mkdir()
    for index in range(2):
        (dense / f"frame-{index:06d}.jpg").touch()
    (dense / "frame-timestamps.json").write_text(
        '{"frame-000000.jpg": 5.0, "frame-000001.jpg": 5.5}\n',
        encoding="utf-8",
    )
    layout = [{
        "kind": "participant_panel", "box": [0.75, 0.0, 0.25, 1.0],
        "start": 5.0, "end": 10.0,
    }]
    with patch.dict(os.environ, {"PRIV_SANITIZER_WHITEBOARD": "0"}), patch.dict(
        sys.modules, {"cv2": FakeCV2()}
    ):
        regions, evidence, _ = detect_face_regions(
            [], layout, 10.0, face_frame_paths=sorted(dense.glob("frame-*.jpg"))
        )
    assert evidence["layout_transition_windows"] >= 1
    assert any(
        item["kind"] == "participant_fullscreen"
        and item["start"] < 5.0
        and item["box"] == [0.0, 0.0, 1.0, 1.0]
        for item in regions
    )


def test_moving_physical_face_stays_one_track_and_has_no_temporal_gap():
    common = {
        "panel": False,
        "detector": "profile",
        "variant": "gray",
        "luma": 120.0,
        "raw_width": 0.10,
        "raw_height": 0.12,
        "raw_y": 0.30,
    }
    observations = [
        {**common, "timestamp": 0.0, "segment_start": 0.0, "segment_end": 0.125,
         "box": [0.10, 0.30, 0.10, 0.12]},
        {**common, "timestamp": 0.25, "segment_start": 0.125, "segment_end": 0.375,
         "box": [0.30, 0.30, 0.10, 0.12]},
    ]
    tracks = _face_tracks(observations)
    assert len(tracks) == 1
    groups = _face_observation_groups(tracks[0], 1.0)
    assert len(groups) == 2
    assert groups[0][0] == 0.0
    assert groups[0][1] == groups[1][0]
    assert groups[-1][1] > groups[0][1]


def test_whiteboard_top_false_positive_does_not_join_face_track():
    common = {
        "panel": False,
        "detector": "profile",
        "variant": "gray",
        "luma": 120.0,
        "raw_width": 0.10,
        "raw_height": 0.12,
        "raw_y": 0.30,
    }
    observations = [
        {**common, "timestamp": 0.0, "segment_start": 0.0, "segment_end": 0.125,
         "box": [0.10, 0.30, 0.10, 0.12]},
        {**common, "timestamp": 0.25, "segment_start": 0.125, "segment_end": 0.375,
         "box": [0.25, 0.02, 0.10, 0.12]},
    ]
    assert len(_face_tracks(observations)) == 2


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

    with patch.dict(os.environ, {"PRIV_SANITIZER_SCENE_DETECTION": "0"}), patch(
        "priv_sanitizer.media.run", side_effect=fake_run
    ):
        frames = sample_frames(Path("input.mp4"), tmp_path, every=4.0, duration=8.0)

    assert [path.name for path in frames] == ["frame-000000.jpg", "frame-000001.jpg"]
    timestamps = __import__("json").loads((tmp_path / "frame-timestamps.json").read_text())
    assert timestamps == {"frame-000000.jpg": 0.0, "frame-000001.jpg": 4.0}


def test_dense_face_sampling_keeps_frame_diff_timestamps(tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        output = Path(args[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.parent.joinpath("frame-000001.jpg").write_bytes(b"first")
        output.parent.joinpath("frame-000002.jpg").write_bytes(b"change")
        return __import__("subprocess").CompletedProcess(
            args,
            0,
            stderr="[showinfo] pts_time:0.000\n[showinfo] pts_time:0.250\n",
        )

    with patch("priv_sanitizer.media.run", side_effect=fake_run):
        frames = sample_dense_face_frames(Path("input.mp4"), tmp_path, duration=1.0)
    assert [path.name for path in frames] == ["frame-000001.jpg", "frame-000002.jpg"]
    timestamps = __import__("json").loads((tmp_path / "frame-timestamps.json").read_text())
    assert timestamps == {"frame-000001.jpg": 0.0, "frame-000002.jpg": 0.25}
    filter_arg = next(arg for arg in calls[0] if arg.startswith("fps="))
    assert "select='eq(n,0)+gt(scene," in filter_arg
    assert "showinfo" in filter_arg


def test_scene_change_candidates_keep_transition_edges(tmp_path):
    def fake_run(args, **kwargs):
        for index in range(1, 4):
            tmp_path.joinpath(f"change-{index:06d}.jpg").write_bytes(b"change")
        return __import__("subprocess").CompletedProcess(
            args,
            0,
            stderr="\n".join([
                "[showinfo] pts_time:1.000",
                "[showinfo] pts_time:1.200",
                "[showinfo] pts_time:3.000",
            ]),
        )

    with patch("priv_sanitizer.media.run", side_effect=fake_run):
        selected = _scene_change_frames(Path("input.mp4"), tmp_path, 8.0)

    assert [round(timestamp, 3) for _, timestamp in selected] == [1.0, 1.2, 3.0]
    assert len(list(tmp_path.glob("change-*.jpg"))) == 3


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


def test_progress_status_shows_braille_spinner(monkeypatch):
    stderr = StringIO()
    monkeypatch.setenv("PRIV_SANITIZER_QUIET", "0")
    with redirect_stderr(stderr):
        _progress_status("rendering video", 42)
        _progress_status("rendering video", 42)
        _progress_status("rendering video", 100, final=True)
    message = stderr.getvalue()
    lines = [line for line in message.splitlines() if line]
    assert len(lines) == 3
    assert all("rendering video:" in line and "42.00%" in line for line in lines[:2])
    symbols = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    assert all(any(symbol in line for symbol in symbols) for line in lines[:2])
    assert lines[0].split(": ", 1)[1][0] != lines[1].split(": ", 1)[1][0]
    assert lines[2].endswith("⠿ 100.00%")


def test_progress_status_shows_ballpark_eta(monkeypatch):
    stderr = StringIO()
    stage = "eta progress test"
    monkeypatch.setenv("PRIV_SANITIZER_QUIET", "0")
    monkeypatch.setenv("PRIV_SANITIZER_VERBOSE", "0")
    with patch("priv_sanitizer.media.time.perf_counter", side_effect=[100.0, 110.0, 120.0]), redirect_stderr(stderr):
        _progress_status(stage, 0)
        _progress_status(stage, 50)
        _progress_status(stage, 100, final=True)
    lines = [line for line in stderr.getvalue().splitlines() if line]
    assert "50.00% ETA 10s" in lines[1]
    assert lines[2].endswith("⠿ 100.00%")


def test_progress_eta_does_not_drift_on_spinner_heartbeat(monkeypatch):
    stderr = StringIO()
    stage = "stable progress test"
    monkeypatch.setenv("PRIV_SANITIZER_QUIET", "0")
    with patch("priv_sanitizer.media.time.perf_counter", side_effect=[100.0, 110.0, 210.0]), redirect_stderr(stderr):
        _progress_status(stage, 0)
        _progress_status(stage, 50)
        _progress_status(stage, 50)
    lines = [line for line in stderr.getvalue().splitlines() if line]
    assert "50.00% ETA 10s" in lines[1]
    assert "50.00% ETA 10s" in lines[2]


def test_progress_status_keeps_fractional_percentage(monkeypatch):
    stderr = StringIO()
    monkeypatch.setenv("PRIV_SANITIZER_QUIET", "0")
    with redirect_stderr(stderr):
        _progress_status("rendering video", 35.05)
    line = stderr.getvalue().strip()
    assert line.endswith("35.05%")


def test_visual_analysis_reports_progress_milestones():
    progress = []
    with patch(
        "priv_sanitizer.media.detect_layout_regions",
        return_value=([], [], {}),
    ), patch(
        "priv_sanitizer.media.detect_ocr_regions",
        return_value=([], {}, []),
    ), patch(
        "priv_sanitizer.media.detect_face_regions",
        return_value=([], {}, []),
    ):
        detect_visual_regions([], 1.0, 1920, 1080, progress=progress.append)
    assert progress[0] == 0
    assert progress[1] == 20
    assert 60 in progress or 90 in progress
    assert progress[-1] == 100


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


def test_fullscreen_regions_do_not_bridge_scene_gaps():
    first = {
        "kind": "participant_fullscreen", "box": [0.0, 0.0, 1.0, 1.0],
        "start": 0.0, "end": 1.0, "confidence": 0.7,
    }
    second = {
        "kind": "participant_fullscreen", "box": [0.0, 0.0, 1.0, 1.0],
        "start": 4.0, "end": 5.0, "confidence": 0.7,
    }
    merged = _merge_regions([first, second])
    assert len(merged) == 2


def test_non_panel_intervals_clip_face_detection_at_rail_boundary():
    layout = [{
        "kind": "participant_panel", "box": [0.75, 0.0, 0.25, 1.0],
        "start": 5.0, "end": 10.0,
    }]
    assert _non_panel_intervals(2.0, 12.0, layout) == [(2.0, 5.0), (10.0, 12.0)]


def test_fullscreen_transition_bridge_survives_panel_coalescing():
    regions = _coalesce_render_regions([
        {
            "kind": "participant_fullscreen", "box": [0.0, 0.0, 1.0, 1.0],
            "start": 0.0, "end": 5.0,
        },
        {
            "kind": "participant_fullscreen", "box": [0.0, 0.0, 1.0, 1.0],
            "start": 4.75, "end": 5.75,
            "sources": ["layout-transition-safety"],
        },
        {
            "kind": "participant_panel", "box": [0.75, 0.0, 0.25, 1.0],
            "start": 5.0, "end": 10.0,
        },
    ])
    fullscreen = [item for item in regions if item["kind"] == "participant_fullscreen"]
    panel = [item for item in regions if item["kind"] == "participant_panel"]
    assert fullscreen and fullscreen[0]["end"] >= 5.75
    assert panel and panel[0]["start"] == 5.0
