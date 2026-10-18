from pathlib import Path
from unittest.mock import patch

from priv_sanitizer import pipeline
from priv_sanitizer.cli import build_parser


def test_no_args_defaults_to_batch_in_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args([])
    assert args.command == "batch"
    assert args.input == tmp_path
    assert args.output == tmp_path / "outputs_sanitized"


def test_jobs_option_accepts_global_and_batch_forms():
    assert build_parser().parse_args(["-j", "3"]).jobs == 3
    assert build_parser().parse_args(["batch", "--jobs", "5"]).jobs == 5


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

    def fake_render(job: Path, output_dir: Path):
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
    assert render.call_count == 2
    assert (tmp_path / "artifacts" / "batch-report.json").exists()
