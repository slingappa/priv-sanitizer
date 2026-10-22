from pathlib import Path
import json
import os
from unittest.mock import patch

from priv_sanitizer import pipeline
from priv_sanitizer.cli import _blur_strength, build_parser


def test_no_args_defaults_to_batch_in_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args([])
    assert args.command == "batch"
    assert args.input == tmp_path
    assert args.output == tmp_path / "outputs_sanitized"


def test_no_argument_main_uses_quick_batch_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    observed = {}
    env_keys = ("PRIV_SANITIZER_FFMPEG_THREADS", "PRIV_SANITIZER_BLUR_STRENGTH")
    previous_env = {key: os.environ.get(key) for key in env_keys}

    def fake_batch(*args, **kwargs):
        observed.update(kwargs)
        return {"completed_count": 0}

    try:
        with patch("priv_sanitizer.cli.batch", side_effect=fake_batch):
            from priv_sanitizer.cli import main

            main([])

        assert observed["keep_artifacts"] is False
        assert observed["every"] == 4.0
        assert os.environ["PRIV_SANITIZER_FFMPEG_THREADS"] == "8"
        assert os.environ["PRIV_SANITIZER_BLUR_STRENGTH"] == "0.3"
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_jobs_option_accepts_global_and_batch_forms():
    assert build_parser().parse_args(["-j", "3"]).jobs == 3
    assert build_parser().parse_args(["batch", "--jobs", "5"]).jobs == 5
    assert build_parser().parse_args(["-k"]).keep_artifacts is True
    assert build_parser().parse_args(["batch", "--keep-artifacts"]).keep_artifacts is True
    assert build_parser().parse_args(["batch", "--keep-videos"]).keep_artifacts is True
    assert build_parser().parse_args(["-b", "48%"]).blur_strength == 0.48
    assert build_parser().parse_args(["--blur-strength", "0.75"]).blur_strength == 0.75
    assert build_parser().parse_args(["batch", "--blur-strength", "75%"]).blur_strength == 0.75
    assert build_parser().parse_args(["--face-padding", "20%"]).face_padding == 0.20
    assert build_parser().parse_args(["batch", "--face-padding", "0.12"]).face_padding == 0.12
    assert build_parser().parse_args(["--no-local-staging"]).local_staging is False
    assert build_parser().parse_args(["batch", "--no-local-staging"]).local_staging is False
    assert build_parser().parse_args(["--aggressive"]).aggressive is True
    assert build_parser().parse_args(["batch", "--balanced"]).aggressive is False
    assert build_parser().parse_args(["-w"]).whiteboard is True
    assert build_parser().parse_args(["batch", "-w"]).whiteboard is True
    assert build_parser().parse_args([]).whiteboard is False
    assert build_parser().parse_args([]).local_staging is None


def test_batch_options_are_available_in_no_argument_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args([
        "--input", str(tmp_path),
        "--output", "deliveries",
        "--artifacts", "audit",
        "--experimental-audio",
        "--every", "2",
    ])
    assert args.command == "batch"
    assert args.input == tmp_path
    assert args.output == Path("deliveries")
    assert args.artifacts == Path("audit")
    assert args.experimental_audio is True
    assert args.every == 2.0


def test_subcommand_arguments_have_help_text():
    parser = build_parser()
    for command, required_fragments in {
        "batch": ("--input", "--artifacts", "--every"),
        "inspect": ("--job",),
        "analyze": ("--audio-mode", "--every"),
        "sanitize": ("--video", "--output", "--audio-mode"),
        "harden": ("--transcript", "--job", "--transcript-offset"),
        "render": ("--output",),
        "verify": ("--output",),
    }.items():
        subparser = next(action for action in parser._actions if action.dest == "command").choices[command]
        help_text = subparser.format_help()
        assert all(fragment in help_text for fragment in required_fragments), command


def test_blur_strength_rejects_values_outside_range():
    for value in ("-0.1", "1.1", "101%", "not-a-number"):
        try:
            _blur_strength(value)
        except Exception:
            pass
        else:
            raise AssertionError(f"accepted invalid opacity {value!r}")


def test_batch_discards_artifacts_unless_retained(tmp_path):
    def run_batch(root: Path, keep_artifacts: bool):
        root.mkdir(parents=True, exist_ok=True)
        video = root / "meeting.mp4"
        video.touch()

        def fake_baseline(source: Path, job: Path, **kwargs):
            job.mkdir(parents=True, exist_ok=True)
            (job / "redaction-manifest.json").write_text(
                json.dumps({"audio_mode": "preserve", "visual_redactions": [], "audio_redactions": []}),
                encoding="utf-8",
            )

        def fake_render(job: Path, output_dir: Path, **kwargs):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "sanitized.mp4").write_bytes(b"artifact video")
            (output_dir / "render.json").write_text(json.dumps({"verify": {"passed": True}}), encoding="utf-8")
            return {"verify": {"passed": True}}

        with patch.object(pipeline, "baseline", side_effect=fake_baseline), patch.object(
            pipeline, "render_job", side_effect=fake_render
        ):
            report = pipeline.batch(root, root / "outputs_sanitized", keep_artifacts=keep_artifacts)
        artifact_root = root / "artifacts" / "meeting"
        return report, artifact_root

    discarded, discarded_video = run_batch(tmp_path / "discard", False)
    assert discarded["keep_artifacts"] is False
    assert discarded["completed_count"] == 1
    assert (tmp_path / "discard" / "outputs_sanitized" / "meeting.mp4").exists()
    assert not discarded_video.exists()
    assert not (tmp_path / "discard" / "artifacts").exists()

    kept, kept_video = run_batch(tmp_path / "keep", True)
    assert kept["keep_artifacts"] is True
    assert kept["completed_count"] == 1
    assert kept_video.exists()
    assert (kept_video / "baseline" / "sanitized.mp4").exists()
    assert (tmp_path / "keep" / "artifacts" / "batch-report.json").exists()


def test_discovery_and_transcript_pairing(tmp_path):
    first = tmp_path / "Meeting A.MP4"
    second = tmp_path / "meeting-b.mov"
    first.touch()
    second.touch()
    (tmp_path / "Meeting A.docx").touch()
    (tmp_path / "unrelated.srt").touch()
    (tmp_path / ".hidden.mp4").touch()
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "ignored.mp4").touch()

    videos = pipeline.discover_videos(tmp_path, tmp_path / "outputs")
    transcripts = pipeline.discover_transcripts(tmp_path)
    assert [path.name for path in videos] == ["Meeting A.MP4", "meeting-b.mov"]
    assert pipeline.match_transcript(videos[0], transcripts, video_count=2)[0].name == "Meeting A.docx"
    matched, warnings = pipeline.match_transcript(videos[1], transcripts, video_count=2)
    assert matched is None
    assert warnings


def test_identical_hardening_redactions_can_reuse_baseline_render():
    base = {
        "audio_mode": "preserve",
        "visual_redactions": [{
            "id": "baseline-id", "kind": "participant_panel", "mode": "blur",
            "box": [0.75, 0.0, 0.25, 1.0], "start": 0.0, "end": 10.0,
        }],
        "audio_redactions": [],
    }
    hardened = {
        **base,
        "visual_redactions": [{
            "id": "transcript-id", "kind": "participant_panel", "mode": "blur",
            "box": [0.75, 0.0, 0.25, 1.0], "start": 0.0, "end": 10.0,
            "sources": ["transcript-assisted"],
        }],
    }
    assert pipeline._can_reuse_baseline_render(base, hardened)
    hardened["audio_mode"] = "mute-names"
    assert not pipeline._can_reuse_baseline_render(base, hardened)


def test_batch_renders_transcript_hardened_manifest_once(tmp_path):
    video = tmp_path / "meeting.mp4"
    transcript = tmp_path / "meeting.srt"
    video.touch()
    transcript.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    visual = [{
        "id": "id", "kind": "participant_panel", "mode": "blur",
        "box": [0.75, 0.0, 0.25, 1.0], "start": 0.0, "end": 1.0,
    }]

    def fake_baseline(source: Path, job: Path, **kwargs):
        job.mkdir(parents=True, exist_ok=True)
        manifest = {
            "source": {"video": str(source)}, "audio_mode": "preserve",
            "visual_redactions": visual, "audio_redactions": [],
        }
        (job / "redaction-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (job / "analysis.json").write_text("{}", encoding="utf-8")
        (job / "probe.json").write_text("{}", encoding="utf-8")

    def fake_render(job: Path, output_dir: Path, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "sanitized.mp4").write_bytes(b"baseline")
        return {"verify": {"passed": True}}

    def fake_harden(baseline_job: Path, transcript_path: Path, output_job: Path, **kwargs):
        output_job.mkdir(parents=True, exist_ok=True)
        manifest = {
            "source": {"video": str(video)}, "audio_mode": "preserve",
            "visual_redactions": [{**visual[0], "id": "hardened-id"}],
            "audio_redactions": [],
        }
        (output_job / "redaction-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with patch.object(pipeline, "baseline", side_effect=fake_baseline), patch.object(
        pipeline, "render_job", side_effect=fake_render
    ) as render, patch.object(pipeline, "harden", side_effect=fake_harden):
        report = pipeline.batch(tmp_path, tmp_path / "outputs_sanitized")
    assert report["completed_count"] == 1
    assert render.call_count == 1
    assert report["results"][0]["baseline"]["rendered"] is False
    assert report["results"][0]["hardened"]["rendered"] is True
    assert report["results"][0]["render_pass"] == "hardened"


def test_batch_runs_both_passes_and_writes_report(tmp_path):
    good = tmp_path / "good.mp4"
    bad = tmp_path / "bad.mp4"
    good.touch()
    bad.touch()
    (tmp_path / "good.docx").touch()

    def fake_baseline(video: Path, job: Path, **kwargs):
        if video.name == "bad.mp4":
            raise RuntimeError("synthetic baseline failure")
        job.mkdir(parents=True, exist_ok=True)

    def fake_render(job: Path, output_dir: Path, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "sanitized.mp4").write_bytes(b"fake video")
        return {"verify": {"passed": True}}

    def fake_harden(baseline_job: Path, transcript: Path, output_job: Path, **kwargs):
        output_job.mkdir(parents=True, exist_ok=True)

    with patch.object(pipeline, "baseline", side_effect=fake_baseline), patch.object(
        pipeline, "render_job", side_effect=fake_render
    ) as render, patch.object(pipeline, "harden", side_effect=fake_harden) as harden:
        report = pipeline.batch(tmp_path, tmp_path / "outputs_sanitized")

    assert report["video_count"] == 2
    assert report["completed_count"] == 1
    assert report["failed_count"] == 1
    by_source = {Path(item["source"]).name: item for item in report["results"]}
    assert by_source["good.mp4"]["hardened"]["status"] == "completed"
    assert Path(by_source["good.mp4"]["final_output"]).exists()
    assert by_source["bad.mp4"]["status"] == "failed"
    assert harden.call_count == 1
    # The transcript-bearing recording is encoded once from its hardened
    # manifest; the failed recording never reaches rendering.
    assert render.call_count == 1
    assert not (tmp_path / "artifacts").exists()


def test_batch_persists_state_and_resumes_after_interrupt(tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"source-one")
    second.write_bytes(b"source-two")
    calls = []
    interrupt_once = True

    def fake_baseline(video: Path, job: Path, **kwargs):
        nonlocal interrupt_once
        calls.append(("baseline", video.name))
        if video.name == "second.mp4" and interrupt_once:
            interrupt_once = False
            raise KeyboardInterrupt
        job.mkdir(parents=True, exist_ok=True)

    def fake_render(job: Path, output_dir: Path, **kwargs):
        calls.append(("render", job.parent.parent.name))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "sanitized.mp4").write_bytes(b"complete-output")
        return {"verify": {"passed": True}}

    with patch.object(pipeline, "baseline", side_effect=fake_baseline), patch.object(
        pipeline, "render_job", side_effect=fake_render
    ):
        try:
            pipeline.batch(tmp_path, tmp_path / "outputs_sanitized", keep_artifacts=True)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("the synthetic interruption did not propagate")

    state_path = tmp_path / "outputs_sanitized" / pipeline.BATCH_STATE_FILENAME
    interrupted = json.loads(state_path.read_text(encoding="utf-8"))
    assert interrupted["status"] == "running"
    assert [item["status"] for item in interrupted["videos"]] == ["completed", "running"]
    assert (tmp_path / "outputs_sanitized" / "first.mp4").exists()
    # Older resumable states predate the local-staging setting; they must
    # still migrate and resume rather than forcing a full restart.
    interrupted["settings"].pop("local_staging", None)
    state_path.write_text(json.dumps(interrupted), encoding="utf-8")

    calls.clear()
    with patch.object(pipeline, "baseline", side_effect=fake_baseline), patch.object(
        pipeline, "render_job", side_effect=fake_render
    ), patch.object(pipeline, "_output_is_complete", side_effect=lambda source, output: output.exists()):
        report = pipeline.batch(tmp_path, tmp_path / "outputs_sanitized", keep_artifacts=True)

    assert report["resumed"] is True
    assert report["completed_count"] == 2
    assert [name for kind, name in calls if kind == "baseline"] == ["second.mp4"]
    completed = json.loads(state_path.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"
    assert all(item["status"] == "completed" for item in completed["videos"])
    assert report["results"][0]["resumed"] is True
