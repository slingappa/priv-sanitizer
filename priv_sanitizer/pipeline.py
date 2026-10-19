"""Job orchestration and manifest/report handling."""

from __future__ import annotations

import copy
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .media import (
    _status,
    audio_levels,
    detect_speech_intervals,
    detect_visual_regions,
    probe,
    render,
    sample_frames,
)
from .transcript import hardening_intervals, parse as parse_transcript


VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"})
TRANSCRIPT_EXTENSIONS = frozenset({".docx", ".srt", ".vtt"})


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def discover_videos(input_dir: Path, output_dir: Path | None = None) -> list[Path]:
    """Discover supported video files directly inside *input_dir*."""
    input_dir = input_dir.expanduser().resolve()
    excluded = output_dir.expanduser().resolve() if output_dir else None
    if not input_dir.is_dir():
        raise ValueError(f"input directory does not exist or is not a directory: {input_dir}")
    return sorted(
        (
            path for path in input_dir.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in VIDEO_EXTENSIONS
            and (excluded is None or path.resolve() != excluded)
        ),
        key=lambda path: path.name.casefold(),
    )


def discover_transcripts(input_dir: Path) -> list[Path]:
    """Discover supported transcript files directly inside *input_dir*."""
    input_dir = input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        raise ValueError(f"input directory does not exist or is not a directory: {input_dir}")
    return sorted(
        (
            path for path in input_dir.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in TRANSCRIPT_EXTENSIONS
        ),
        key=lambda path: path.name.casefold(),
    )


def match_transcript(
    video: Path,
    transcripts: list[Path],
    *,
    video_count: int,
) -> tuple[Path | None, list[str]]:
    """Pair a video with a transcript without guessing across a batch."""
    warnings: list[str] = []
    same_stem = [path for path in transcripts if path.stem.casefold() == video.stem.casefold()]
    if len(same_stem) == 1:
        return same_stem[0], warnings
    if len(same_stem) > 1:
        warnings.append(
            "multiple same-stem transcripts found; transcript hardening was skipped: "
            + ", ".join(path.name for path in same_stem)
        )
        return None, warnings
    if video_count == 1 and len(transcripts) == 1:
        return transcripts[0], warnings
    if transcripts:
        warnings.append("no unambiguous matching transcript found; transcript hardening was skipped")
    return None, warnings


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def baseline(
    video: Path,
    job: Path,
    *,
    audio_mode: str = "preserve",
    every: float = 4.0,
    analyze_audio: bool | None = None,
) -> dict[str, Any]:
    video = video.resolve()
    job.mkdir(parents=True, exist_ok=True)
    _status("probing input")
    info = probe(video)
    write_json(job / "probe.json", info)
    stage_started = time.perf_counter()
    frames = sample_frames(video, job / "frames", every=every, duration=info["duration"])
    _status(f"sampling complete: {len(frames)} frames in {time.perf_counter() - stage_started:.1f}s")
    stage_started = time.perf_counter()
    _status("analysing layout, OCR, and faces")
    visual, visual_warnings, detector_evidence = detect_visual_regions(
        frames, info["duration"], info["width"], info["height"]
    )
    _status(f"visual analysis complete: {len(visual)} regions in {time.perf_counter() - stage_started:.1f}s")
    if analyze_audio is None:
        analyze_audio = (
            audio_mode != "preserve"
            or os.environ.get("PRIV_SANITIZER_AUDIO_ANALYSIS", "").lower() in {"1", "true", "yes", "on"}
        )
    speech_activity: list[dict[str, Any]] = []
    audio_warnings: list[str] = []
    if analyze_audio:
        stage_started = time.perf_counter()
        _status("analysing audio")
        speech_activity, audio_warnings = detect_speech_intervals(video, info["duration"])
        _status(f"audio analysis complete in {time.perf_counter() - stage_started:.1f}s")
    else:
        _status("audio analysis skipped (audio preserved; enable --experimental-audio or PRIV_SANITIZER_AUDIO_ANALYSIS=1 to enable)")
        audio_warnings.append("speech activity analysis skipped because audio processing is disabled")
    audio = []
    if audio_mode != "preserve":
        audio_warnings.append(
            "baseline cannot identify spoken names from speech activity alone; audio remains preserved; use transcript hardening"
        )
    if info["has_audio"]:
        audio_warnings.append("audio is preserved in the baseline; spoken participant-name privacy remains unresolved")
    manifest = {
        "schema_version": 1,
        "created_at": now(),
        "source": {"video": str(video), "video_name": video.name},
        "policy": "conservative",
        "pass": "baseline",
        "analysis_sources": ["video", "layout", "ocr", "face"] + (["vad-evidence"] if analyze_audio else []),
        "audio_mode": audio_mode,
        "video": {k: info[k] for k in ("width", "height", "duration", "fps")},
        "protected_regions": [],
        "visual_redactions": visual,
        "audio_redactions": audio,
        "review": {"required": True, "approved": False, "unresolved_intervals": []},
    }
    analysis = {
        "pass": "baseline",
        "video": info,
        "frame_samples": len(frames),
        "visual_detections": visual,
        "audio_detections": audio,
        "speech_activity": speech_activity,
        "detectors": detector_evidence,
        "warnings": visual_warnings + audio_warnings,
    }
    _status("writing analysis manifest")
    write_json(job / "analysis.json", analysis)
    write_json(job / "redaction-manifest.json", manifest)
    write_report(job, analysis, manifest)
    return manifest


def harden(
    baseline_job: Path,
    transcript: Path,
    output_job: Path,
    *,
    narrow_audio: bool = False,
    transcript_offset: float = 0.0,
) -> dict[str, Any]:
    base = read_json(baseline_job / "redaction-manifest.json")
    analysis = read_json(baseline_job / "analysis.json")
    info = read_json(baseline_job / "probe.json")
    _status("analysing optional transcript")
    transcript_data = parse_transcript(transcript)
    transcript_visual: list[dict[str, Any]] = []
    transcript_visual_warnings: list[str] = []
    transcript_detectors: dict[str, Any] = {}
    frame_dir = baseline_job / "frames"
    if frame_dir.exists():
        transcript_frames = sorted(frame_dir.glob("frame-*.jpg"))
        if transcript_frames:
            transcript_visual, transcript_visual_warnings, transcript_detectors = detect_visual_regions(
                transcript_frames,
                info["duration"],
                info["width"],
                info["height"],
                known_names=transcript_data["names"],
            )
    candidate_audio = hardening_intervals(
        transcript_data, info["duration"], time_offset=transcript_offset
    )
    manifest = copy.deepcopy(base)
    manifest["created_at"] = now()
    manifest["pass"] = "hardened"
    manifest["analysis_sources"] = list(dict.fromkeys([*manifest.get("analysis_sources", []), "transcript"]))
    manifest["source"]["transcript"] = str(transcript.resolve())
    if transcript_visual:
        manifest["visual_redactions"] = transcript_visual
    manifest["transcript_analysis"] = {
        "format": transcript_data["format"],
        "cue_count": len(transcript_data["cues"]),
        "participant_name_count": len(transcript_data["names"]),
        "candidate_audio_interval_count": len(candidate_audio),
        "time_offset": transcript_offset,
        "experimental_audio_redaction_requested": narrow_audio,
    }
    warnings: list[str] = []
    if candidate_audio and narrow_audio:
        manifest["audio_mode"] = "mute-names"
        manifest["audio_redactions"] = candidate_audio
    elif not candidate_audio:
        manifest["audio_mode"] = "preserve"
        manifest["audio_redactions"] = []
        warnings.append("transcript supplied no usable spoken-name intervals; audio was preserved")
    else:
        manifest["audio_mode"] = "preserve"
        manifest["audio_redactions"] = []
        warnings.append("narrow name muting was disabled; audio was preserved")
    hardened_analysis = {
        **analysis,
        "pass": "hardened",
        "transcript": {
            "path": str(transcript.resolve()),
            "format": transcript_data["format"],
            "cue_count": len(transcript_data["cues"]),
            "participant_names": transcript_data["names"],
            "candidate_audio_intervals": candidate_audio,
            "time_offset": transcript_offset,
        },
        "warnings": [*analysis.get("warnings", []), *warnings],
    }
    if transcript_visual_warnings:
        hardened_analysis["warnings"].extend(transcript_visual_warnings)
    if transcript_detectors:
        hardened_analysis["detectors"] = transcript_detectors
        hardened_analysis["visual_detections"] = transcript_visual
    output_job.mkdir(parents=True, exist_ok=True)
    write_json(output_job / "probe.json", info)
    write_json(output_job / "analysis.json", hardened_analysis)
    write_json(output_job / "redaction-manifest.json", manifest)
    write_report(output_job, hardened_analysis, manifest)
    return manifest


def write_report(job: Path, analysis: dict[str, Any], manifest: dict[str, Any]) -> None:
    report = {
        "job": str(job.resolve()),
        "pass": manifest.get("pass"),
        "visual_redactions": len(manifest.get("visual_redactions", [])),
        "audio_redactions": len(manifest.get("audio_redactions", [])),
        "audio_mode": manifest.get("audio_mode"),
        "warnings": analysis.get("warnings", []),
        "review_required": manifest.get("review", {}).get("required", True),
        "detectors": analysis.get("detectors", {}),
    }
    write_json(job / "report.json", report)
    lines = [
        "priv-sanitizer report",
        f"pass: {report['pass']}",
        f"audio mode: {report['audio_mode']}",
        f"visual redactions: {report['visual_redactions']}",
        f"audio redactions: {report['audio_redactions']}",
        "",
        "Warnings:",
    ]
    lines.extend(f"- {warning}" for warning in report["warnings"] or ["none"])
    (job / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_job(
    job: Path,
    output_dir: Path,
    *,
    verify_audio: bool | None = None,
) -> dict[str, Any]:
    _status("preparing render")
    manifest = read_json(job / "redaction-manifest.json")
    source = Path(manifest["source"]["video"])
    output_dir.mkdir(parents=True, exist_ok=True)
    result = render(source, manifest, output_dir / "sanitized.mp4")
    if verify_audio is None:
        verify_audio = (
            manifest.get("audio_mode") != "preserve"
            or os.environ.get("PRIV_SANITIZER_AUDIO_ANALYSIS", "").lower() in {"1", "true", "yes", "on"}
        )
    verify = verify_output(source, output_dir / "sanitized.mp4", audio_metrics=verify_audio)
    write_json(output_dir / "render.json", {**result, "verify": verify})
    return {**result, "verify": verify}


def _mark_artifact_video_retention(output_dir: Path, retained: bool) -> None:
    """Record whether the rendered artifact MP4 remains on disk."""
    metadata_path = output_dir / "render.json"
    if not metadata_path.exists():
        return
    try:
        metadata = read_json(metadata_path)
    except (OSError, ValueError, TypeError):
        return
    metadata["artifact_video_retained"] = retained
    write_json(metadata_path, metadata)


def _discard_artifact_video(output_dir: Path) -> None:
    """Remove only the generated per-pass MP4, preserving audit metadata."""
    video_path = output_dir / "sanitized.mp4"
    try:
        video_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _status(f"warning: could not remove artifact video {video_path}: {exc}")
    _mark_artifact_video_retention(output_dir, False)


def _redaction_signature(manifest: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    """Return render-relevant redactions without detector IDs/evidence noise."""
    signature: list[tuple[Any, ...]] = []
    for item in manifest.get("visual_redactions", []):
        signature.append((
            "visual",
            item.get("kind"),
            item.get("mode", "blur"),
            tuple(round(float(value), 5) for value in item.get("box", [])),
            round(float(item.get("start", 0.0)), 3),
            round(float(item.get("end", 0.0)), 3),
        ))
    for item in manifest.get("audio_redactions", []):
        signature.append((
            "audio",
            round(float(item.get("start", 0.0)), 3),
            round(float(item.get("end", 0.0)), 3),
        ))
    return tuple(sorted(signature))


def _can_reuse_baseline_render(baseline: dict[str, Any], hardened: dict[str, Any]) -> bool:
    """Avoid a second encode when hardening did not change the rendered mask."""
    if baseline.get("audio_mode") != hardened.get("audio_mode"):
        return False
    return _redaction_signature(baseline) == _redaction_signature(hardened)


def reuse_render_job(
    baseline_output: Path,
    hardened_job: Path,
    hardened_output_dir: Path,
    *,
    verify_audio: bool | None = None,
) -> dict[str, Any]:
    """Materialize a hardened artifact by reusing an identical baseline encode."""
    _status("reusing baseline render; transcript hardening did not change redactions")
    manifest = read_json(hardened_job / "redaction-manifest.json")
    source = Path(manifest["source"]["video"])
    hardened_output_dir.mkdir(parents=True, exist_ok=True)
    output = hardened_output_dir / "sanitized.mp4"
    shutil.copy2(baseline_output, output)
    if verify_audio is None:
        verify_audio = (
            manifest.get("audio_mode") != "preserve"
            or os.environ.get("PRIV_SANITIZER_AUDIO_ANALYSIS", "").lower() in {"1", "true", "yes", "on"}
        )
    verify = verify_output(source, output, audio_metrics=verify_audio)
    result = {
        "output": str(output),
        "visual_filters": len(manifest.get("visual_redactions", [])),
        "audio_filters": len(manifest.get("audio_redactions", [])),
        "audio_mode": manifest.get("audio_mode"),
        "render_seconds": 0.0,
        "reused_baseline": True,
        "verify": verify,
    }
    write_json(hardened_output_dir / "render.json", result)
    return result


def batch(
    input_dir: Path,
    output_dir: Path,
    *,
    artifacts_dir: Path | None = None,
    experimental_audio: bool = False,
    keep_videos: bool = False,
    every: float = 4.0,
) -> dict[str, Any]:
    """Run baseline plus optional transcript hardening for every local video."""
    input_dir = input_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    artifacts_dir = (artifacts_dir or output_dir.parent / "artifacts").expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    videos = discover_videos(input_dir, output_dir)
    transcripts = discover_transcripts(input_dir)
    results: list[dict[str, Any]] = []
    used_output_keys: set[str] = set()

    for index, video in enumerate(videos, start=1):
        output_key = video.stem
        suffix = 2
        while output_key.casefold() in used_output_keys:
            output_key = f"{video.stem}-{suffix}"
            suffix += 1
        used_output_keys.add(output_key.casefold())
        video_root = artifacts_dir / output_key
        baseline_dir = video_root / "baseline"
        baseline_job = baseline_dir / "job"
        transcript, pairing_warnings = match_transcript(
            video, transcripts, video_count=len(videos)
        )
        entry: dict[str, Any] = {
            "source": str(video),
            "output_key": output_key,
            "transcript": str(transcript) if transcript else None,
            "experimental_audio_redaction": experimental_audio,
            "warnings": pairing_warnings.copy(),
            "baseline": {"status": "pending"},
            "hardened": {"status": "skipped", "reason": "no matching transcript"},
            "final_output": None,
        }
        final_source: Path | None = None
        try:
            _status(f"batch {index}/{len(videos)}: baseline {video.name}")
            baseline(
                video,
                baseline_job,
                audio_mode="preserve",
                every=every,
                analyze_audio=experimental_audio,
            )
            baseline_render = render_job(baseline_job, baseline_dir, verify_audio=experimental_audio)
            entry["baseline"] = {
                "status": "completed" if baseline_render["verify"]["passed"] else "failed",
                "output": str((baseline_dir / "sanitized.mp4").resolve()),
                "artifact_video_retained": keep_videos,
                "verify": baseline_render["verify"],
            }
            final_source = baseline_dir / "sanitized.mp4"

            if transcript:
                hardened_dir = video_root / "hardened"
                hardened_job = hardened_dir / "job"
                try:
                    _status(f"batch {index}/{len(videos)}: hardening {video.name}")
                    harden(
                        baseline_job,
                        transcript,
                        hardened_job,
                        narrow_audio=experimental_audio,
                        transcript_offset=0.0,
                    )
                    baseline_manifest_path = baseline_job / "redaction-manifest.json"
                    hardened_manifest_path = hardened_job / "redaction-manifest.json"
                    if baseline_manifest_path.exists() and hardened_manifest_path.exists() and _can_reuse_baseline_render(
                        read_json(baseline_manifest_path), read_json(hardened_manifest_path)
                    ):
                        hardened_render = reuse_render_job(
                            baseline_dir / "sanitized.mp4",
                            hardened_job,
                            hardened_dir,
                            verify_audio=experimental_audio,
                        )
                    else:
                        _status("hardening changed render redactions; rendering hardened output")
                        hardened_render = render_job(hardened_job, hardened_dir, verify_audio=experimental_audio)
                    entry["hardened"] = {
                        "status": "completed" if hardened_render["verify"]["passed"] else "failed",
                        "output": str((hardened_dir / "sanitized.mp4").resolve()),
                        "artifact_video_retained": keep_videos,
                        "verify": hardened_render["verify"],
                        "reused_baseline": hardened_render.get("reused_baseline", False),
                    }
                    if hardened_render["verify"]["passed"]:
                        final_source = hardened_dir / "sanitized.mp4"
                    else:
                        entry["warnings"].append("hardened render failed verification; baseline output was selected")
                except Exception as exc:  # keep independent videos processing
                    entry["hardened"] = {"status": "failed", "error": str(exc)}
                    entry["warnings"].append("transcript hardening failed; baseline output was selected")

            if final_source is None or not final_source.exists():
                raise RuntimeError("no rendered output was produced")
            # Keep detailed audit artifacts in the per-video directory, while
            # exposing a simple flat final-file contract for batch callers:
            # output_dir/<input-stem>.mp4.
            final_output = output_dir / f"{output_key}.mp4"
            # A prior per-video failure or an external cleanup process must not
            # remove the delivery directory for later videos in the batch.
            final_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(final_source, final_output)
            entry["final_output"] = str(final_output.resolve())
            entry["status"] = "completed" if entry["baseline"]["status"] == "completed" else "failed"
        except Exception as exc:  # one bad recording must not stop the batch
            entry["status"] = "failed"
            entry["error"] = str(exc)
            _status(f"batch {index}/{len(videos)}: failed {video.name}: {exc}")
        finally:
            if not keep_videos:
                # The flat output has already been copied when processing
                # succeeds. Keep JSON/TXT manifests and reports, but avoid
                # retaining another full encoded video under artifacts.
                _discard_artifact_video(baseline_dir)
                _discard_artifact_video(video_root / "hardened")
        results.append(entry)

    report = {
        "schema_version": 1,
        "created_at": now(),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "artifacts_dir": str(artifacts_dir),
        "experimental_audio_redaction": experimental_audio,
        "keep_artifact_videos": keep_videos,
        "video_count": len(videos),
        "completed_count": sum(item.get("status") == "completed" for item in results),
        "failed_count": sum(item.get("status") == "failed" for item in results),
        "results": results,
        "warnings": ["no supported videos found"] if not videos else [],
    }
    write_json(artifacts_dir / "batch-report.json", report)
    return report


def verify_output(
    source: Path,
    output: Path,
    *,
    audio_metrics: bool | None = None,
) -> dict[str, Any]:
    src = probe(source)
    dst = probe(output)
    duration_delta = abs(src["duration"] - dst["duration"])
    if audio_metrics is None:
        audio_metrics = os.environ.get("PRIV_SANITIZER_AUDIO_ANALYSIS", "").lower() in {"1", "true", "yes", "on"}
    if audio_metrics:
        source_audio = audio_levels(source) if src["has_audio"] else {"mean_db": None, "max_db": None}
        output_audio = audio_levels(output) if dst["has_audio"] else {"mean_db": None, "max_db": None}
    else:
        _status("audio level verification skipped (audio presence and duration are still checked)")
        source_audio = {"mean_db": None, "max_db": None}
        output_audio = {"mean_db": None, "max_db": None}
    audio_presence_ok = not src["has_audio"] or dst["has_audio"]
    result = {
        "source_duration": src["duration"],
        "output_duration": dst["duration"],
        "duration_delta": round(duration_delta, 3),
        "duration_ok": duration_delta <= 1.0,
        "output_has_video": dst["width"] > 0 and dst["height"] > 0,
        "output_has_audio": dst["has_audio"],
        "audio_presence_ok": audio_presence_ok,
        "audio_levels_checked": audio_metrics,
        "source_audio_levels": source_audio,
        "output_audio_levels": output_audio,
        "passed": duration_delta <= 1.0 and dst["width"] > 0 and dst["height"] > 0 and audio_presence_ok,
    }
    return result
