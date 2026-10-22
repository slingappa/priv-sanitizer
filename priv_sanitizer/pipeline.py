"""Job orchestration and manifest/report handling."""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .media import (
    ToolError,
    _status,
    _progress_status,
    _coalesce_render_regions,
    adaptive_sampling_interval,
    aggressive_redaction_enabled,
    audio_levels,
    detect_speech_intervals,
    detect_visual_regions,
    detector_capabilities,
    dense_face_sampling_enabled,
    dense_face_sampling_fps,
    dense_face_diff_threshold,
    dense_face_fallback_seconds,
    effective_privacy_settings,
    transition_timestamps_from_samples,
    whiteboard_mode_enabled,
    probe,
    render,
    sample_dense_face_frames,
    sample_frames,
)
from .transcript import hardening_intervals, parse as parse_transcript


VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"})
TRANSCRIPT_EXTENSIONS = frozenset({".docx", ".srt", ".vtt"})
BATCH_STATE_FILENAME = ".priv-sanitizer-batch.json"
BATCH_STATE_SCHEMA_VERSION = 1


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_atomic_json(path: Path, value: Any) -> None:
    """Write a small state file atomically so a kill cannot corrupt it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


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
    source_display: Path | None = None,
) -> dict[str, Any]:
    video = video.resolve()
    display_video = (source_display or video).expanduser().resolve()
    job.mkdir(parents=True, exist_ok=True)
    _status("probing input")
    info = probe(video)
    # Keep audit manifests tied to the user's source path even when the
    # decoder is reading a local staged copy.
    info["path"] = str(display_video)
    write_json(job / "probe.json", info)
    stage_started = time.perf_counter()
    sample_every = adaptive_sampling_interval(every, info["duration"])
    frames = sample_frames(video, job / "frames", every=sample_every, duration=info["duration"])
    _status(f"sampling complete: {len(frames)} frames in {time.perf_counter() - stage_started:.1f}s")
    dense_face_frames: list[Path] = []
    transition_timestamps = transition_timestamps_from_samples(frames)
    face_capability = detector_capabilities()["face"]
    dense_faces_requested = dense_face_sampling_enabled()
    dense_faces_enabled = (
        dense_faces_requested
        and bool(face_capability.get("available"))
        and sample_dense_face_frames is not None
    )
    if dense_faces_enabled:
        stage_started = time.perf_counter()
        dense_face_frames = sample_dense_face_frames(
            video,
            job / "dense_faces",
            duration=info["duration"],
            transition_timestamps=transition_timestamps,
        )
        _status(f"dense face sampling complete: {len(dense_face_frames)} frames in {time.perf_counter() - stage_started:.1f}s")
    elif dense_faces_requested and not face_capability.get("available"):
        _status(f"dense face sampling skipped ({face_capability.get('reason') or 'face backend unavailable'})")
    stage_started = time.perf_counter()
    analysis_stage = "analysing layout, OCR, and faces"
    visual, visual_warnings, detector_evidence = detect_visual_regions(
        frames, info["duration"], info["width"], info["height"],
        face_frame_paths=dense_face_frames or None,
        progress=lambda percent: _progress_status(
            analysis_stage, percent, final=percent >= 100
        ),
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
    privacy_settings = effective_privacy_settings()
    manifest = {
        "schema_version": 1,
        "created_at": now(),
        "source": {"video": str(display_video), "video_name": display_video.name},
        "policy": (
            "experimental-whiteboard-localized-participant-regions"
            if privacy_settings["whiteboard"] else "conservative"
        ),
        "pass": "baseline",
        "analysis_sources": ["video", "layout", "ocr", "face"] + (["vad-evidence"] if analyze_audio else []),
        "audio_mode": audio_mode,
        "privacy": {
            **privacy_settings,
            "aggressive_redaction": aggressive_redaction_enabled(),
        },
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
        "requested_sampling_interval": round(float(every), 3),
        "sampling_interval": round(sample_every, 3),
        "privacy": {
            **privacy_settings,
            "aggressive_redaction": aggressive_redaction_enabled(),
        },
        "dense_face_sampling": {
            "enabled": dense_faces_enabled,
            "requested": dense_faces_requested,
            "fps": dense_face_sampling_fps() if dense_faces_enabled else None,
            "diff_threshold": dense_face_diff_threshold() if dense_faces_enabled else None,
            "fallback_seconds": dense_face_fallback_seconds() if dense_faces_enabled else None,
            "frame_count": len(dense_face_frames),
            "transition_count": len(transition_timestamps),
            "sampling_plan": read_json(job / "dense_faces" / "sampling.json")
            if (job / "dense_faces" / "sampling.json").exists() else None,
            "reason": None if dense_faces_enabled else (
                face_capability.get("reason") if dense_faces_requested else "disabled by configuration"
            ),
        },
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
            transcript_stage = "analysing transcript layout, OCR, and faces"
            transcript_visual, transcript_visual_warnings, transcript_detectors = detect_visual_regions(
                transcript_frames,
                info["duration"],
                info["width"],
                info["height"],
                known_names=transcript_data["names"],
                progress=lambda percent: _progress_status(
                    transcript_stage, percent, final=percent >= 100
                ),
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
        # Transcript OCR is secondary evidence.  Merge it with the baseline
        # detector output instead of replacing the baseline, otherwise a
        # transcript pass that sees only a subset of sampled frames could
        # discard face/rail protections already found in pass one.
        manifest["visual_redactions"] = _coalesce_render_regions(
            [*manifest.get("visual_redactions", []), *transcript_visual]
        )
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
        # Keep baseline detector evidence (including the dense face scan) and
        # record the transcript-guided recheck separately for auditability.
        hardened_analysis["transcript_detectors"] = transcript_detectors
        hardened_analysis["visual_detections"] = manifest.get("visual_redactions", [])
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
    source_override: Path | None = None,
) -> dict[str, Any]:
    _status("preparing render")
    manifest = read_json(job / "redaction-manifest.json")
    source = (source_override or Path(manifest["source"]["video"])).expanduser().resolve()
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


def _discard_artifact_tree(video_root: Path, artifacts_dir: Path) -> None:
    """Remove one generated batch artifact tree, never an arbitrary parent."""
    root = video_root.resolve()
    parent = artifacts_dir.resolve()
    if root.parent != parent:
        raise ValueError(f"refusing to remove artifact path outside batch root: {root}")
    if root.exists():
        shutil.rmtree(root)


def _file_fingerprint(path: Path | None) -> dict[str, int] | None:
    """Return a cheap identity for a source/transcript file."""
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _batch_settings(*, experimental_audio: bool, every: float) -> dict[str, Any]:
    """Capture output-affecting settings used to decide whether to resume."""
    return {
        "experimental_audio": bool(experimental_audio),
        "every": round(float(every), 3),
        "privacy": effective_privacy_settings(),
    }


def _state_matches(
    state: dict[str, Any],
    *,
    input_dir: Path,
    output_dir: Path,
    artifacts_dir: Path,
    settings: dict[str, Any],
) -> bool:
    stored_settings = state.get("settings")
    # State files created before local staging was introduced did not carry
    # that key. They are compatible with the current renderer; normalize the
    # missing field so an interrupted older batch can still resume.
    if isinstance(stored_settings, dict) and "local_staging" not in stored_settings:
        stored_settings = {**stored_settings, "local_staging": settings.get("local_staging")}
    return (
        state.get("schema_version") == BATCH_STATE_SCHEMA_VERSION
        and state.get("input_dir") == str(input_dir)
        and state.get("output_dir") == str(output_dir)
        and state.get("artifacts_dir") == str(artifacts_dir)
        and stored_settings == settings
        and isinstance(state.get("videos"), list)
    )


def _load_batch_state(
    path: Path,
    *,
    input_dir: Path,
    output_dir: Path,
    artifacts_dir: Path,
    settings: dict[str, Any],
) -> dict[str, Any] | None:
    """Load a compatible state file; malformed/stale state starts a new job."""
    if not path.is_file():
        return None
    try:
        state = read_json(path)
    except (OSError, ValueError, TypeError):
        _status("ignoring unreadable batch state; starting a new job")
        return None
    if not isinstance(state, dict) or not _state_matches(
        state,
        input_dir=input_dir,
        output_dir=output_dir,
        artifacts_dir=artifacts_dir,
        settings=settings,
    ):
        _status("existing batch state does not match this input/settings; starting a new job")
        return None
    return state


def _output_is_complete(source: Path, output: Path) -> bool:
    """Reject missing/partial delivery files before marking an item resumed."""
    if not output.is_file():
        return False
    try:
        if output.stat().st_size <= 0:
            return False
        source_info = probe(source)
        output_info = probe(output)
    except (OSError, ToolError, ValueError, KeyError, TypeError):
        return False
    return (
        abs(float(source_info["duration"]) - float(output_info["duration"])) <= 1.0
        and int(output_info.get("width", 0)) > 0
        and int(output_info.get("height", 0)) > 0
        and (not source_info.get("has_audio") or output_info.get("has_audio"))
    )


def _publish_output(source: Path, destination: Path) -> None:
    """Copy a final video without ever exposing a partially copied MP4."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part")
    try:
        temporary.unlink(missing_ok=True)
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _local_staging_enabled(value: bool | None) -> bool:
    """Return whether batch inputs should be copied to local scratch first."""
    if value is not None:
        return bool(value)
    configured = os.environ.get("PRIV_SANITIZER_LOCAL_STAGING", "1").lower()
    return configured not in {"0", "false", "no", "off"}


def _stage_batch_inputs(
    video: Path,
    transcript: Path | None,
) -> tuple[Path, Path, Path | None]:
    """Copy one batch item to local scratch so analysis rereads stay local."""
    base_dir = Path(os.environ.get("PRIV_SANITIZER_STAGE_DIR", tempfile.gettempdir())).expanduser()
    base_dir.mkdir(parents=True, exist_ok=True)
    source_size = video.stat().st_size
    free_bytes = shutil.disk_usage(base_dir).free
    # Frames and detector sidecars can temporarily consume roughly another
    # copy of a large recording. Fall back to direct processing rather than
    # filling /tmp and failing halfway through a batch.
    if free_bytes < source_size * 2 + 512 * 1024 * 1024:
        raise OSError(
            f"local staging needs about {source_size * 2 / (1024 ** 3):.1f} GiB; "
            f"only {free_bytes / (1024 ** 3):.1f} GiB is free"
        )
    stage_root = Path(tempfile.mkdtemp(prefix="priv-sanitizer-", dir=base_dir))
    try:
        staged_video = stage_root / "input" / video.name
        staged_video.parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        _status(f"staging input locally: {video.name}")
        shutil.copy2(video, staged_video)
        _status(
            f"local staging complete: {source_size / (1024 ** 2):.0f} MiB in "
            f"{time.perf_counter() - started:.1f}s"
        )
        staged_transcript: Path | None = None
        if transcript:
            staged_transcript = stage_root / "input" / transcript.name
            shutil.copy2(transcript, staged_transcript)
        return stage_root, staged_video, staged_transcript
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def _retain_staged_artifacts(local_root: Path, network_root: Path) -> None:
    """Copy audit material back and rewrite temporary paths in JSON traces."""
    network_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(local_root, network_root, dirs_exist_ok=True)
    local_prefix = str(local_root.resolve())
    network_prefix = str(network_root.resolve())
    for json_path in network_root.rglob("*.json"):
        try:
            text = json_path.read_text(encoding="utf-8")
            rewritten = text.replace(local_prefix, network_prefix)
            if rewritten != text:
                json_path.write_text(rewritten, encoding="utf-8")
        except (OSError, UnicodeError):
            continue


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
        "face_padding": manifest.get("privacy", {}).get("face_padding"),
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
    keep_artifacts: bool = False,
    keep_videos: bool | None = None,
    every: float = 4.0,
    local_staging: bool | None = None,
) -> dict[str, Any]:
    """Run a batch and resume completed items after interruption."""
    # ``keep_videos`` is retained as a Python API compatibility alias. The
    # batch retention decision now covers all generated audit artifacts, not
    # only MP4s, because those files can still consume substantial storage.
    if keep_videos is not None:
        keep_artifacts = keep_videos
    input_dir = input_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    artifacts_dir = (artifacts_dir or output_dir.parent / "artifacts").expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Batch diagnostics travel with the deliveries, not the caller's cwd.
    os.environ["PRIV_SANITIZER_VERBOSE_LOG"] = str(output_dir / "verbose.log")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    videos = discover_videos(input_dir, output_dir)
    transcripts = discover_transcripts(input_dir)
    settings = _batch_settings(experimental_audio=experimental_audio, every=every)
    settings["keep_artifacts"] = bool(keep_artifacts)
    settings["local_staging"] = _local_staging_enabled(local_staging)
    state_path = output_dir / BATCH_STATE_FILENAME
    previous_state = _load_batch_state(
        state_path,
        input_dir=input_dir,
        output_dir=output_dir,
        artifacts_dir=artifacts_dir,
        settings=settings,
    )
    used_output_keys: set[str] = set()
    plans: list[tuple[Path, str, Path | None]] = []
    for video in videos:
        output_key = video.stem
        suffix = 2
        while output_key.casefold() in used_output_keys:
            output_key = f"{video.stem}-{suffix}"
            suffix += 1
        used_output_keys.add(output_key.casefold())
        transcript, _ = match_transcript(video, transcripts, video_count=len(videos))
        plans.append((video, output_key, transcript))

    old_by_source = {
        item.get("source"): item
        for item in (previous_state or {}).get("videos", [])
        if isinstance(item, dict) and item.get("source")
    }
    # A completed state is treated as resumable only when a delivery is
    # missing or malformed; otherwise an explicit repeat keeps old behavior.
    state_needs_resume = bool(previous_state) and (
        previous_state.get("status") != "completed"
        or any(
            not _output_is_complete(video, output_dir / f"{output_key}.mp4")
            for video, output_key, _ in plans
            if old_by_source.get(str(video))
        )
    )
    if state_needs_resume:
        _status("resuming previous batch from output state")
    reusable_state = previous_state if state_needs_resume else None
    state: dict[str, Any] = {
        "schema_version": BATCH_STATE_SCHEMA_VERSION,
        "created_at": (previous_state or {}).get("created_at", now()),
        "updated_at": now(),
        "status": "running",
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "artifacts_dir": str(artifacts_dir),
        "settings": settings,
        "current_source": None,
        "videos": [],
    }
    for video, output_key, transcript in plans:
        old = old_by_source.get(str(video)) if reusable_state else None
        same_inputs = bool(old) and (
            old.get("source_fingerprint") == _file_fingerprint(video)
            and old.get("transcript") == (str(transcript) if transcript else None)
            and old.get("transcript_fingerprint") == _file_fingerprint(transcript)
        )
        status = "pending"
        final_output = output_dir / f"{output_key}.mp4"
        if (
            same_inputs
            and old.get("status") in {"completed", "running"}
            and (not keep_artifacts or old.get("artifacts_retained") is True)
        ):
            if _output_is_complete(video, final_output):
                status = "completed"
        state["videos"].append({
            "source": str(video),
            "source_fingerprint": _file_fingerprint(video),
            "transcript": str(transcript) if transcript else None,
            "transcript_fingerprint": _file_fingerprint(transcript),
            "output_key": output_key,
            "final_output": str(final_output),
            "status": status,
            "stage": None,
            "started_at": old.get("started_at") if same_inputs and old else None,
            "completed_at": old.get("completed_at") if status == "completed" and old else None,
            "artifacts_retained": old.get("artifacts_retained") if status == "completed" and old else False,
            "error": None,
        })
    _write_atomic_json(state_path, state)

    def persist_state() -> None:
        state["updated_at"] = now()
        _write_atomic_json(state_path, state)

    results: list[dict[str, Any]] = []

    for index, ((video, output_key, planned_transcript), state_entry) in enumerate(
        zip(plans, state["videos"]), start=1
    ):
        network_video_root = artifacts_dir / output_key
        video_root = network_video_root
        baseline_dir = video_root / "baseline"
        baseline_job = baseline_dir / "job"
        transcript, pairing_warnings = match_transcript(
            video, transcripts, video_count=len(videos)
        )
        if state_entry.get("status") == "completed":
            _status(f"batch {index}/{len(videos)}: reusing {video.name}")
            if not keep_artifacts:
                _discard_artifact_tree(video_root, artifacts_dir)
            results.append({
                "source": str(video),
                "output_key": output_key,
                "transcript": str(planned_transcript) if planned_transcript else None,
                "experimental_audio_redaction": experimental_audio,
                "warnings": pairing_warnings.copy(),
                "baseline": {"status": "reused", "rendered": False},
                "hardened": {
                    "status": "reused" if planned_transcript else "skipped",
                    "reason": "resumed completed output" if planned_transcript else "no matching transcript",
                },
                "final_output": str((output_dir / f"{output_key}.mp4").resolve()),
                "render_pass": "resumed",
                "resumed": True,
                "status": "completed",
            })
            continue
        state_entry.update({
            "status": "running",
            "stage": "staging" if _local_staging_enabled(local_staging) else "baseline-analysis",
            "started_at": now(),
            "error": None,
        })
        state["current_source"] = str(video)
        state["current_index"] = index
        persist_state()
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
        stage_root: Path | None = None
        processing_video = video
        processing_transcript = transcript
        try:
            if _local_staging_enabled(local_staging):
                try:
                    stage_root, processing_video, processing_transcript = _stage_batch_inputs(
                        video, transcript
                    )
                    video_root = stage_root / "artifacts" / output_key
                    baseline_dir = video_root / "baseline"
                    baseline_job = baseline_dir / "job"
                except OSError as exc:
                    entry["warnings"].append(f"local staging unavailable; processing source directly: {exc}")
                    _status(f"local staging unavailable; processing source directly: {video.name}")
                    state_entry["stage"] = "baseline-analysis"
                    persist_state()
            else:
                state_entry["stage"] = "baseline-analysis"
                persist_state()
            _status(f"batch {index}/{len(videos)}: baseline {video.name}")
            baseline(
                processing_video,
                baseline_job,
                audio_mode="preserve",
                every=every,
                analyze_audio=experimental_audio,
                source_display=video,
            )
            entry["baseline"] = {
                "status": "completed",
                "rendered": False,
                "output": None,
                "artifact_video_retained": keep_artifacts,
                "artifacts_retained": keep_artifacts,
            }

            if transcript:
                hardened_dir = video_root / "hardened"
                hardened_job = hardened_dir / "job"
                try:
                    state_entry["stage"] = "transcript-hardening"
                    persist_state()
                    _status(f"batch {index}/{len(videos)}: hardening {video.name}")
                    harden(
                        baseline_job,
                        processing_transcript or transcript,
                        hardened_job,
                        narrow_audio=experimental_audio,
                        transcript_offset=0.0,
                    )
                    # Both analysis passes complete before encoding. The
                    # transcript-aware manifest is the final decision, so a
                    # matching baseline MP4 is no longer rendered first and
                    # then replaced by a second full-length encode.
                    state_entry["stage"] = "rendering"
                    persist_state()
                    _status("rendering transcript-hardened output (single pass)")
                    hardened_render = render_job(
                        hardened_job,
                        hardened_dir,
                        verify_audio=experimental_audio,
                        source_override=processing_video,
                    )
                    entry["hardened"] = {
                        "status": "completed" if hardened_render["verify"]["passed"] else "failed",
                        "rendered": True,
                        "output": str((network_video_root / "hardened" / "sanitized.mp4").resolve()),
                        "artifact_video_retained": keep_artifacts,
                        "artifacts_retained": keep_artifacts,
                        "verify": hardened_render["verify"],
                    }
                    if hardened_render["verify"]["passed"]:
                        final_source = hardened_dir / "sanitized.mp4"
                        entry["render_pass"] = "hardened"
                        entry["baseline"]["reason"] = "baseline analysis retained; render deferred until transcript hardening"
                    else:
                        entry["warnings"].append("hardened render failed verification; rendering baseline output")
                except Exception as exc:  # keep independent videos processing
                    entry["hardened"] = {"status": "failed", "error": str(exc)}
                    entry["warnings"].append("transcript hardening/render failed; rendering baseline output")

            if final_source is None:
                # No transcript was paired, or the transcript pass failed
                # verification. Encode the baseline exactly once as fallback.
                state_entry["stage"] = "rendering"
                persist_state()
                baseline_render = render_job(
                    baseline_job,
                    baseline_dir,
                    verify_audio=experimental_audio,
                    source_override=processing_video,
                )
                entry["baseline"] = {
                    "status": "completed" if baseline_render["verify"]["passed"] else "failed",
                    "rendered": True,
                    "output": str((network_video_root / "baseline" / "sanitized.mp4").resolve()),
                    "artifact_video_retained": keep_artifacts,
                    "artifacts_retained": keep_artifacts,
                    "verify": baseline_render["verify"],
                }
                final_source = baseline_dir / "sanitized.mp4"
                entry["render_pass"] = "baseline"

            if final_source is None or not final_source.exists():
                raise RuntimeError("no rendered output was produced")
            if stage_root is not None and keep_artifacts:
                state_entry["stage"] = "retaining-artifacts"
                persist_state()
                _retain_staged_artifacts(video_root, network_video_root)
            # Keep detailed audit artifacts in the per-video directory, while
            # exposing a simple flat final-file contract for batch callers:
            # output_dir/<input-stem>.mp4.
            final_output = output_dir / f"{output_key}.mp4"
            # Publish atomically so a killed copy cannot look like a finished
            # delivery on the next invocation.
            state_entry["stage"] = "publishing"
            persist_state()
            _publish_output(final_source, final_output)
            entry["final_output"] = str(final_output.resolve())
            entry["status"] = "completed" if entry["baseline"]["status"] == "completed" else "failed"
            if entry["status"] == "completed":
                state_entry.update({
                    "status": "completed",
                    "stage": None,
                    "artifacts_retained": bool(keep_artifacts),
                    "completed_at": now(),
                    "output_fingerprint": _file_fingerprint(final_output),
                })
            else:
                state_entry["status"] = "failed"
        except Exception as exc:  # one bad recording must not stop the batch
            entry["status"] = "failed"
            entry["error"] = str(exc)
            state_entry.update({"status": "failed", "stage": "failed", "error": str(exc)[:1000]})
            _status(f"batch {index}/{len(videos)}: failed {video.name}: {exc}")
        finally:
            state["current_source"] = None if state_entry.get("status") == "completed" else str(video)
            persist_state()
            if not keep_artifacts:
                # The flat output has already been copied when processing
                # succeeds. Remove the complete generated audit tree unless
                # the caller explicitly requested it for debugging.
                _discard_artifact_tree(network_video_root, artifacts_dir)
            if stage_root is not None:
                shutil.rmtree(stage_root, ignore_errors=True)
        results.append(entry)

    completed_count = sum(item.get("status") == "completed" for item in results)
    failed_count = sum(item.get("status") == "failed" for item in results)
    state["status"] = "completed" if failed_count == 0 else "partial"
    state["current_source"] = None
    state["completed_count"] = completed_count
    state["failed_count"] = failed_count
    state["finished_at"] = now()
    persist_state()

    report = {
        "schema_version": 1,
        "created_at": now(),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "artifacts_dir": str(artifacts_dir),
        "batch_state": str(state_path),
        "resumed": state_needs_resume,
        "experimental_audio_redaction": experimental_audio,
        "experimental_whiteboard": whiteboard_mode_enabled(),
        "keep_artifacts": keep_artifacts,
        # Deprecated report key retained for consumers of older reports.
        "keep_artifact_videos": keep_artifacts,
        "video_count": len(videos),
        "completed_count": completed_count,
        "failed_count": failed_count,
        "results": results,
        "warnings": ["no supported videos found"] if not videos else [],
    }
    batch_report = artifacts_dir / "batch-report.json"
    if keep_artifacts:
        write_json(batch_report, report)
    else:
        # A default batch leaves delivery files and the small resumable state
        # file only. Remove generated audit data from this run.
        try:
            batch_report.unlink()
        except FileNotFoundError:
            pass
        try:
            artifacts_dir.rmdir()
        except OSError:
            # Keep a caller-owned/non-empty directory intact. The generated
            # per-video trees and report have already been removed above.
            pass
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
