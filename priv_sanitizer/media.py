"""FFmpeg probing, local vision analysis, and rendering helpers."""

from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable


class ToolError(RuntimeError):
    pass


_VERBOSE_LOG_INITIALIZED = False
_VERBOSE_LOG_FAILURE_REPORTED = False
_PROGRESS_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_PROGRESS_COMPLETE = "⠿"  # Braille dots 1-6: a complete six-dot block.
_PROGRESS_SPINNER_INDEX = 0
_PROGRESS_SPINNER_INTERVAL = 0.25


def _tool(name: str) -> str:
    override_key = "PRIV_SANITIZER_FFMPEG" if name == "ffmpeg" else "PRIV_SANITIZER_FFPROBE"
    path = os.environ.get(override_key)
    if not path and name == "ffprobe":
        ffmpeg_override = os.environ.get("PRIV_SANITIZER_FFMPEG")
        if ffmpeg_override:
            sibling = Path(ffmpeg_override).with_name("ffprobe")
            if sibling.exists():
                path = str(sibling)
    if not path:
        tool_dir = os.environ.get("PRIV_SANITIZER_FFMPEG_DIR")
        if tool_dir:
            candidate = Path(tool_dir) / name
            if candidate.exists():
                path = str(candidate)
    if not path:
        path = shutil.which(name)
    if path and not Path(path).exists():
        path = None
    if not path:
        raise ToolError(
            f"{name} was not found. Install FFmpeg, add it to PATH, or set "
            f"PRIV_SANITIZER_FFMPEG_DIR (or PRIV_SANITIZER_{name.upper()})"
        )
    return path


def _status(message: str) -> None:
    if os.environ.get("PRIV_SANITIZER_QUIET") != "1":
        print(f"[priv-sanitizer] {message}", file=os.sys.stderr, flush=True)
    _verbose_log(f"status: {message}")


def _verbose_enabled() -> bool:
    configured = os.environ.get("PRIV_SANITIZER_VERBOSE", "1").lower()
    return (
        os.environ.get("PRIV_SANITIZER_QUIET") != "1"
        and configured not in {"0", "false", "no", "off"}
    )


def _verbose_log(message: str) -> None:
    """Append diagnostics to the caller's working-directory log when enabled."""
    global _VERBOSE_LOG_INITIALIZED, _VERBOSE_LOG_FAILURE_REPORTED
    if not _verbose_enabled():
        return
    path = Path(os.environ.get("PRIV_SANITIZER_VERBOSE_LOG", "verbose.log"))
    try:
        with path.open("a", encoding="utf-8") as stream:
            if not _VERBOSE_LOG_INITIALIZED:
                stream.write(
                    f"=== priv-sanitizer verbose session {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ===\n"
                )
                _VERBOSE_LOG_INITIALIZED = True
            stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")
    except OSError as exc:
        if not _VERBOSE_LOG_FAILURE_REPORTED:
            print(f"[priv-sanitizer] WARNING: could not write verbose log {path}: {exc}", file=os.sys.stderr, flush=True)
            _VERBOSE_LOG_FAILURE_REPORTED = True


def _verbose(message: str) -> None:
    if _verbose_enabled():
        rendered = f"verbose: {message}"
        # Detailed commands and heartbeat text are intentionally log-only.
        # Keep terminal output limited to concise stage/progress status so a
        # long filter graph cannot flood stdout/stderr.
        _verbose_log(rendered)


def _heartbeat_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("PRIV_SANITIZER_HEARTBEAT_SECONDS", "15")))
    except ValueError:
        return 15.0


def _progress_status(stage: str, percent: int, *, final: bool = False) -> None:
    global _PROGRESS_SPINNER_INDEX
    if os.environ.get("PRIV_SANITIZER_QUIET") == "1":
        return
    if final:
        spinner = _PROGRESS_COMPLETE
    else:
        spinner = _PROGRESS_SPINNER[_PROGRESS_SPINNER_INDEX % len(_PROGRESS_SPINNER)]
        _PROGRESS_SPINNER_INDEX += 1
    message = f"[priv-sanitizer] {stage}: {spinner} {percent:3d}%"
    _verbose_log(f"progress: {stage}: {percent:3d}%")
    stream = os.sys.stderr
    if stream.isatty():
        suffix = "\033[K\n" if final else "\033[K"
        print(f"\r{message}{suffix}", file=stream, end="", flush=True)
    else:
        print(message, file=stream, flush=True)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Stop a child process promptly when the user cancels a run."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2.0)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


def run(
    args: list[str], *, capture: bool = True, progress_stage: str | None = None,
    duration: float | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    if _verbose_enabled():
        _verbose(f"starting ({progress_stage or 'command'}): {shlex.join(args)}")
    if progress_stage:
        progress_args = [args[0], "-progress", "pipe:1", *args[1:]]
        try:
            process = subprocess.Popen(
                progress_args,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ToolError(str(exc)) from exc
        last_percent = -1
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, data="stdout")
        if process.stderr is not None:
            # Drain FFmpeg diagnostics concurrently. Filters such as
            # silencedetect can emit enough stderr on long recordings to fill
            # the pipe; waiting until stdout closes would deadlock FFmpeg.
            selector.register(process.stderr, selectors.EVENT_READ, data="stderr")
        last_progress_at = started
        last_status_at = started
        heartbeat = _heartbeat_seconds()
        # Keep the interactive spinner responsive without making
        # non-interactive logs noisy. Heartbeat diagnostics remain on their
        # slower configured cadence in both modes.
        spinner_interval = min(heartbeat, _PROGRESS_SPINNER_INTERVAL) if os.sys.stderr.isatty() else heartbeat
        last_heartbeat_log_at = started
        try:
            while selector.get_map():
                events = selector.select(timeout=spinner_interval)
                if not events:
                    if process.poll() is None:
                        now = time.perf_counter()
                        elapsed = time.perf_counter() - started
                        quiet_for = time.perf_counter() - last_progress_at
                        if last_percent >= 0 and now - last_status_at >= spinner_interval:
                            _progress_status(progress_stage, last_percent)
                            last_status_at = now
                        if now - last_heartbeat_log_at >= heartbeat:
                            _verbose(
                                f"{progress_stage} still running after {elapsed:.0f}s "
                                f"(no FFmpeg progress for {quiet_for:.0f}s; last reported {last_percent if last_percent >= 0 else 'none'}%)"
                            )
                            last_heartbeat_log_at = now
                    continue
                for key, _ in events:
                    line = key.fileobj.readline()
                    if not line:
                        selector.unregister(key.fileobj)
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    if key.data == "stderr":
                        stderr_lines.append(line)
                        continue
                    stdout_lines.append(line)
                    match = re.match(r"out_time_ms=(\d+)", line)
                    if match and duration and duration > 0:
                        last_progress_at = time.perf_counter()
                        percent = min(99, int((int(match.group(1)) / 1_000_000) / duration * 100))
                        if percent != last_percent:
                            _progress_status(progress_stage, percent)
                            last_percent = percent
                            last_status_at = time.perf_counter()
                # Some FFmpeg filters emit diagnostics continuously while their
                # encoded timestamp remains unchanged. Keep the spinner moving in
                # that case too, without pretending that percentage advanced.
                if process.poll() is None and last_percent >= 0:
                    now = time.perf_counter()
                    if now - last_status_at >= spinner_interval:
                        _progress_status(progress_stage, last_percent)
                        last_status_at = now
        except KeyboardInterrupt:
            _terminate_process(process)
            selector.close()
            raise
        selector.close()
        stderr = "\n".join(stderr_lines)
        returncode = process.wait()
        if returncode != 0:
            detail = (stderr or "\n".join(stdout_lines)).strip()
            raise ToolError(f"command failed ({returncode}): {' '.join(args)}\n{detail[-4000:]}")
        if last_percent >= 0:
            _progress_status(progress_stage, 100, final=True)
        _verbose(f"finished ({progress_stage}) in {time.perf_counter() - started:.1f}s")
        return subprocess.CompletedProcess(progress_args, returncode, "\n".join(stdout_lines), stderr)
    if _verbose_enabled():
        try:
            process = subprocess.Popen(
                args,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )
        except OSError as exc:
            raise ToolError(str(exc)) from exc
        try:
            stdout, stderr = process.communicate(timeout=_heartbeat_seconds())
        except KeyboardInterrupt:
            _terminate_process(process)
            raise
        except subprocess.TimeoutExpired:
            while True:
                elapsed = time.perf_counter() - started
                _verbose(f"command still running after {elapsed:.0f}s: {shlex.join(args)}")
                try:
                    stdout, stderr = process.communicate(timeout=_heartbeat_seconds())
                    break
                except KeyboardInterrupt:
                    _terminate_process(process)
                    raise
                except subprocess.TimeoutExpired:
                    continue
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ToolError(f"command failed ({result.returncode}): {' '.join(args)}\n{detail[-4000:]}")
        _verbose(f"finished (command) in {time.perf_counter() - started:.1f}s")
        return result
    try:
        result = subprocess.run(args, text=True, capture_output=capture, check=False)
    except OSError as exc:
        raise ToolError(str(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ToolError(f"command failed ({result.returncode}): {' '.join(args)}\n{detail[-4000:]}")
    return result


def probe(video: Path) -> dict[str, Any]:
    result = run([
        _tool("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(video),
    ])
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = float(data.get("format", {}).get("duration") or video_stream.get("duration") or 0)
    fps_text = video_stream.get("r_frame_rate", "0/1")
    try:
        num, den = fps_text.split("/", 1)
        fps = float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "path": str(video.resolve()),
        "duration": round(duration, 3),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "fps": round(fps, 6),
        "video_codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name"),
        "audio_channels": audio_stream.get("channels"),
        "audio_sample_rate": audio_stream.get("sample_rate"),
        "has_audio": bool(audio_stream),
        "format": data.get("format", {}).get("format_name"),
    }


def adaptive_sampling_interval(every: float, duration: float | None) -> float:
    """Choose a denser cadence for short clips without penalizing long meetings.

    The normal four-second cadence is appropriate for long Meet recordings when
    the scene-change trigger is also enabled.  Short/mobile clips can contain a
    presenter or nameplate transition between those samples, so cap the
    interval at 2 fps for clips up to 30 seconds and 1 fps for clips up to two
    minutes.  Callers can restore the requested cadence with
    ``PRIV_SANITIZER_ADAPTIVE_SAMPLING=0``.
    """
    requested = max(float(every), 0.25)
    if not whiteboard_mode_enabled():
        return requested
    configured = os.environ.get("PRIV_SANITIZER_ADAPTIVE_SAMPLING", "1").lower()
    if configured in {"0", "false", "no", "off"} or duration is None or duration <= 0:
        return requested
    if duration <= 30.0:
        return min(requested, 0.5)
    if duration <= 120.0:
        return min(requested, 1.0)
    return requested


def dense_face_sampling_enabled() -> bool:
    if not whiteboard_mode_enabled():
        return False
    configured = os.environ.get("PRIV_SANITIZER_DENSE_FACE_SAMPLING", "1").lower()
    return configured not in {"0", "false", "no", "off"}


def dense_face_sampling_fps() -> float:
    try:
        return max(0.5, min(8.0, float(os.environ.get("PRIV_SANITIZER_DENSE_FACE_FPS", "4"))))
    except ValueError:
        return 4.0


def dense_face_diff_threshold() -> float:
    """Return the normalized FFmpeg frame-difference trigger threshold."""
    try:
        return max(0.001, min(1.0, float(os.environ.get("PRIV_SANITIZER_FACE_DIFF_THRESHOLD", "0.02"))))
    except ValueError:
        return 0.02


def dense_face_fallback_seconds() -> float:
    """Bound gaps when a scene metric misses a gradual face movement."""
    try:
        return max(0.25, min(5.0, float(os.environ.get("PRIV_SANITIZER_FACE_FALLBACK_SECONDS", "1.0"))))
    except ValueError:
        return 1.0


def dense_face_windowing_enabled() -> bool:
    """Enable transition-window face sampling for long recordings."""
    configured = os.environ.get("PRIV_SANITIZER_DENSE_FACE_WINDOWING", "0").lower()
    return configured in {"1", "true", "yes", "on"}


def dense_face_transition_window_seconds() -> float:
    try:
        return max(0.5, min(30.0, float(os.environ.get("PRIV_SANITIZER_DENSE_FACE_WINDOW_SECONDS", "5"))))
    except ValueError:
        return 5.0


def dense_face_sparse_seconds() -> float:
    """Return the fallback cadence outside transition windows."""
    try:
        return max(1.0, min(30.0, float(os.environ.get("PRIV_SANITIZER_DENSE_FACE_SPARSE_SECONDS", "4"))))
    except ValueError:
        return 4.0


def configured_blur_radius() -> int:
    """Return the FFmpeg luma blur radius used for visual redactions.

    Radius is intentionally configurable because privacy review may prefer a
    stronger mask than the normal 64-pixel default.  It is clamped to a range
    supported by FFmpeg and to a value that remains practical for small crops.
    """
    try:
        return max(1, min(128, int(os.environ.get("PRIV_SANITIZER_BLUR_RADIUS", "64"))))
    except (TypeError, ValueError):
        return 64


def configured_blur_opacity() -> float:
    """Return how much of the blurred layer replaces the original (0..1)."""
    try:
        value = os.environ.get("PRIV_SANITIZER_BLUR_OPACITY", "1.0").strip()
        opacity = float(value[:-1]) / 100.0 if value.endswith("%") else float(value)
        return max(0.0, min(1.0, opacity))
    except (TypeError, ValueError):
        return 1.0


def configured_face_padding() -> float:
    """Return symmetric physical-face expansion as a fraction of the box.

    The same value is applied horizontally and vertically.  It is deliberately
    separate from blur radius: padding controls how far the redaction reaches,
    while radius controls how strongly pixels inside it are blurred.
    """
    try:
        value = os.environ.get("PRIV_SANITIZER_FACE_PADDING", "0.16").strip()
        padding = float(value[:-1]) / 100.0 if value.endswith("%") else float(value)
        return max(0.0, min(0.5, padding))
    except (TypeError, ValueError):
        return 0.16


def whiteboard_mode_enabled() -> bool:
    """Return whether experimental physical-camera/whiteboard mode is active."""
    configured = os.environ.get("PRIV_SANITIZER_WHITEBOARD", "0").lower()
    return configured in {"1", "true", "yes", "on"}


def effective_privacy_settings() -> dict[str, Any]:
    """Return the settings actually used by the selected rendering mode.

    The pre-whiteboard meeting renderer is intentionally kept stable: it uses
    the historical radius-18 opaque blur and has no physical-face padding
    setting.  The newer radius/opacity/padding controls only apply after the
    explicit ``-w`` opt-in, so manifests and reports must not imply that those
    controls affected a normal meeting run.
    """
    if not whiteboard_mode_enabled():
        return {
            "whiteboard": False,
            "blur_radius": 18,
            "blur_opacity": 1.0,
            "face_padding": None,
        }
    return {
        "whiteboard": True,
        "blur_radius": configured_blur_radius(),
        "blur_opacity": configured_blur_opacity(),
        "face_padding": configured_face_padding(),
    }


def aggressive_redaction_enabled() -> bool:
    """Legacy compatibility hook; whole-frame masking is never automatic."""
    # Older manifests and launchers used this setting for a privacy fallback
    # that blurred an entire unknown-layout interval.  That behavior is not a
    # useful participant redaction: it destroys shared content and is outside
    # the product requirement. Keep the hook for schema/CLI compatibility, but
    # deliberately disable it for every run.
    return False


def sample_dense_face_frames(
    video: Path, output_dir: Path, duration: float | None = None,
    *, transition_timestamps: Iterable[float] | None = None,
) -> list[Path]:
    """Extract frame-difference-triggered frames for layout-independent faces.

    The source is sampled at the configured face FPS (4 by default), but only
    frames whose low-resolution FFmpeg scene metric changes, plus a bounded
    periodic fallback, are written. This gives motion-sensitive coverage
    without running Haar cascades on every source frame.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in [
        *output_dir.glob("frame-*.jpg"),
        *output_dir.glob("face-timestamps.json"),
        *output_dir.glob("sampling.json"),
    ]:
        try:
            stale.unlink()
        except OSError:
            pass
    fps = dense_face_sampling_fps()
    threshold = dense_face_diff_threshold()
    # Short camera/whiteboard clips can contain a face motion between every
    # one-second fallback point. Keep the full configured cadence for clips up
    # to 30 seconds, use a 2-fps safety cadence through two minutes, and keep
    # the cheaper one-second fallback for long meetings. Scene-change frames
    # are still added on top of each cadence.
    fallback_seconds = dense_face_fallback_seconds()
    if duration is not None and duration <= 30.0:
        fallback_seconds = min(fallback_seconds, 1.0 / max(fps, 0.5))
    elif duration is not None and duration <= 120.0:
        fallback_seconds = min(fallback_seconds, 0.5)
    transition_values = sorted({
        max(0.0, min(float(duration or timestamp), float(timestamp)))
        for timestamp in (transition_timestamps or [])
        if float(timestamp) >= 0.0
    })
    window_seconds = dense_face_transition_window_seconds()
    windows: list[tuple[float, float]] = []
    for timestamp in transition_values:
        start = max(0.0, timestamp - window_seconds)
        end = min(float(duration or timestamp + window_seconds), timestamp + window_seconds)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    windowed = bool(
        dense_face_windowing_enabled()
        and duration is not None
        and duration > 120.0
        and windows
    )
    if windowed:
        fallback_seconds = max(fallback_seconds, dense_face_sparse_seconds())
    fallback_frames = max(1, round(fps * fallback_seconds))
    pattern = output_dir / "frame-%06d.jpg"
    select_terms = [
        "eq(n,0)",
        f"gt(scene,{threshold:.4f})",
        f"not(mod(n,{fallback_frames}))",
    ]
    if windowed:
        select_terms.extend(
            f"between(t,{start:.3f},{end:.3f})" for start, end in windows
        )
        _verbose(
            f"dense face transition windows: {len(windows)} windows +/-{window_seconds:.1f}s; "
            f"sparse fallback every {fallback_seconds:.1f}s"
        )
    select = "+".join(select_terms)
    result = run([
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "info", "-y",
        "-i", str(video),
        "-vf", f"fps={fps:.3f},scale=640:-2,select='{select}',showinfo",
        "-fps_mode", "vfr", "-q:v", "7", str(pattern),
    ], progress_stage="dense face sampling", duration=duration)
    frames = sorted(output_dir.glob("frame-*.jpg"))
    timestamps_from_log = [
        float(match.group(1))
        for line in (result.stderr or "").splitlines()
        if (match := re.search(r"pts_time:([0-9.]+)", line))
    ]
    if len(timestamps_from_log) != len(frames):
        _verbose(
            f"dense face timestamp/frame mismatch: {len(timestamps_from_log)} timestamps, "
            f"{len(frames)} images; using nominal timestamps for missing entries"
        )
    timestamps = {
        path.name: round(
            timestamps_from_log[index] if index < len(timestamps_from_log) else index / fps,
            3,
        )
        for index, path in enumerate(frames)
    }
    (output_dir / "frame-timestamps.json").write_text(
        json.dumps(timestamps, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "sampling.json").write_text(
        json.dumps({
            "fps": fps,
            "frame_diff_threshold": threshold,
            "fallback_seconds": fallback_seconds,
            "windowed": windowed,
            "window_seconds": window_seconds if windowed else None,
            "transition_count": len(transition_values),
            "window_count": len(windows) if windowed else 0,
            "frame_count": len(frames),
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return frames


def transition_timestamps_from_samples(frame_paths: Iterable[Path]) -> list[float]:
    """Read retained scene-transition timestamps from the regular sampler."""
    paths = list(frame_paths)
    if not paths:
        return []
    metadata = paths[0].parent / "frame-timestamps.json"
    try:
        timestamps = json.loads(metadata.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return []
    return sorted(
        float(value) for name, value in timestamps.items()
        if str(name).startswith("change-")
    )


def sample_frames(
    video: Path, output_dir: Path, every: float = 4.0, duration: float | None = None
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    # FFmpeg's fps filter emits a frame at source time zero.  Decode frame zero
    # explicitly as a privacy guard, then discard that duplicate periodic
    # frame before assigning timestamps.  Keeping the duplicate while labeling
    # it as t=every shifts every later layout boundary by one sampling period.
    for stale in [*output_dir.glob("frame-*.jpg"), *output_dir.glob("periodic-*.jpg")]:
        try:
            stale.unlink()
        except OSError:
            pass
    timestamp_path = output_dir / "frame-timestamps.json"
    try:
        timestamp_path.unlink()
    except FileNotFoundError:
        pass
    first = output_dir / "frame-000000.jpg"
    run([
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-vf", r"select=eq(n\,0)", "-frames:v", "1",
        "-q:v", "3", str(first),
    ], progress_stage=None)
    pattern = output_dir / "periodic-%06d.jpg"
    run([
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-vf", f"fps=1/{max(every, 0.25):.3f}",
        "-q:v", "5", str(pattern),
    ], progress_stage="sampling frames", duration=duration)
    periodic = sorted(output_dir.glob("periodic-*.jpg"))
    if periodic:
        # The first periodic image is the same source-time-zero frame already
        # stored above.  Do not relabel it as t=every; that would make a brief
        # layout transition appear one interval later than it really occurs.
        first_periodic = periodic.pop(0)
        try:
            first_periodic.unlink()
        except OSError:
            pass
    frames = [first]
    timestamps: dict[str, float] = {first.name: 0.0}
    for index, path in enumerate(periodic, start=1):
        target = output_dir / f"frame-{index:06d}.jpg"
        path.replace(target)
        # The periodic filter is treated as a nominal sampler.  Its first
        # frame was discarded above, so this image really corresponds to the
        # first interval boundary (t=every), subject only to normal decoder
        # timestamp quantization.
        timestamps[target.name] = round(index * max(every, 0.25), 3)
        frames.append(target)
    scene_frames = _scene_change_frames(video, output_dir, duration)
    existing_times = list(timestamps.values())
    for path, timestamp in scene_frames:
        if any(abs(timestamp - existing) < 0.25 for existing in existing_times):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        timestamp = round(max(0.0, min(float(duration or timestamp), timestamp)), 3)
        timestamps[path.name] = timestamp
        existing_times.append(timestamp)
        frames.append(path)
    timestamp_path.write_text(json.dumps(timestamps, indent=2) + "\n", encoding="utf-8")
    return frames


def _scene_change_threshold() -> float:
    try:
        return max(0.001, min(1.0, float(os.environ.get("PRIV_SANITIZER_SCENE_THRESHOLD", "0.02"))))
    except ValueError:
        return 0.02


def _scene_change_merge_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("PRIV_SANITIZER_SCENE_MERGE_SECONDS", "0.75")))
    except ValueError:
        return 0.75


def _scene_change_frames(
    video: Path, output_dir: Path, duration: float | None,
) -> list[tuple[Path, float]]:
    """Extract high-resolution frames at FFmpeg scene-change candidates.

    This is a trigger layer, not a privacy decision: the candidates are fed
    through the normal layout/OCR/face detectors.  Keeping the first and last
    frame of each nearby candidate cluster captures both sides of a brief
    participant-layout transition without OCR-running every decoded frame.
    """
    if os.environ.get("PRIV_SANITIZER_SCENE_DETECTION", "1").lower() in {
        "0", "false", "no", "off"
    }:
        return []
    for stale in output_dir.glob("change-*.jpg"):
        try:
            stale.unlink()
        except OSError:
            pass
    threshold = _scene_change_threshold()
    pattern = output_dir / "change-%06d.jpg"
    result = run([
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "info", "-y",
        "-i", str(video),
        "-vf", f"select='gt(scene,{threshold:.4f})',showinfo",
        "-fps_mode", "vfr", "-q:v", "5", str(pattern),
    ], progress_stage="detecting scene changes", duration=duration)
    timestamps = [
        float(match.group(1))
        for line in (result.stderr or "").splitlines()
        if (match := re.search(r"pts_time:([0-9.]+)", line))
    ]
    paths = sorted(output_dir.glob("change-*.jpg"))
    if len(paths) != len(timestamps):
        _verbose(
            f"scene-change timestamp/frame mismatch: {len(timestamps)} timestamps, "
            f"{len(paths)} images; using the common prefix"
        )
    candidates = [
        (path, timestamp)
        for path, timestamp in zip(paths, timestamps)
        if timestamp >= 0.0 and (duration is None or timestamp <= float(duration) + 0.25)
    ]
    if not candidates:
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass
        return []
    merge_seconds = _scene_change_merge_seconds()
    clusters: list[list[tuple[Path, float]]] = []
    for candidate in candidates:
        if not clusters or candidate[1] - clusters[-1][-1][1] > merge_seconds:
            clusters.append([candidate])
        else:
            clusters[-1].append(candidate)
    selected: list[tuple[Path, float]] = []
    keep_paths: set[Path] = set()
    for cluster in clusters:
        selected.extend([cluster[0]] if len(cluster) == 1 else [cluster[0], cluster[-1]])
    keep_paths.update(path for path, _ in selected)
    for path, _ in candidates:
        if path not in keep_paths:
            try:
                path.unlink()
            except OSError:
                pass
    _verbose(
        f"scene-change candidates: {len(candidates)} frames, "
        f"{len(selected)} retained across {len(clusters)} clusters"
    )
    return selected


def _sample_entries(
    frame_paths: Iterable[Path], duration: float, *, every: float = 4.0
) -> list[tuple[Path, float, float, float]]:
    """Return (path, timestamp, segment_start, segment_end) for sampled frames."""
    paths = list(frame_paths)
    if not paths:
        return []
    timestamps: dict[str, float] = {}
    parents = {path.parent for path in paths}
    if len(parents) == 1:
        metadata = next(iter(parents)) / "frame-timestamps.json"
        try:
            raw = json.loads(metadata.read_text(encoding="utf-8"))
            timestamps = {str(key): float(value) for key, value in raw.items()}
        except (FileNotFoundError, OSError, ValueError, TypeError):
            timestamps = {}
    nominal_step = max(every, 0.25)
    if not timestamps:
        # Test callers and older jobs do not have the sidecar.  A uniform
        # fallback still produces temporal regions instead of one global box.
        step = duration / max(len(paths), 1)
        timestamps = {
            path.name: (index * step if len(paths) > 1 else 0.0)
            for index, path in enumerate(paths)
        }
        nominal_step = step or nominal_step
    ordered = sorted(
        ((path, timestamps.get(path.name, index * nominal_step)) for index, path in enumerate(paths)),
        key=lambda item: item[1],
    )
    entries: list[tuple[Path, float, float, float]] = []
    for index, (path, timestamp) in enumerate(ordered):
        timestamp = max(0.0, min(float(duration), float(timestamp)))
        previous = ordered[index - 1][1] if index else timestamp - nominal_step
        following = ordered[index + 1][1] if index + 1 < len(ordered) else timestamp + nominal_step
        start = max(0.0, (float(previous) + timestamp) / 2.0)
        end = min(float(duration), (timestamp + float(following)) / 2.0)
        if index == 0:
            start = 0.0
        if index + 1 == len(ordered):
            end = float(duration)
        if end <= start:
            end = min(float(duration), start + nominal_step)
        entries.append((path, round(timestamp, 3), round(start, 3), round(end, 3)))
    return entries


def detector_capabilities() -> dict[str, Any]:
    """Report optional backend availability explicitly."""
    result: dict[str, Any] = {
        "layout": {"available": True, "backend": "local-layout-v2"},
        "ocr": {"available": False, "backend": "pytesseract", "reason": "not installed"},
        "face": {"available": False, "backend": "opencv-haar", "reason": "not installed"},
    }
    try:
        import pytesseract  # type: ignore
        result["ocr"] = {
            "available": bool(shutil.which("tesseract")),
            "backend": "pytesseract+tesseract",
            "reason": None if shutil.which("tesseract") else "tesseract executable not found",
            "version": getattr(pytesseract, "__version__", None),
        }
    except ImportError:
        pass
    try:
        import cv2  # type: ignore
        required = ("CascadeClassifier", "imread", "cvtColor", "data")
        compatible = all(hasattr(cv2, name) for name in required) and hasattr(cv2.data, "haarcascades")
        result["face"] = {
            "available": compatible,
            "backend": "opencv-haar",
            "version": getattr(cv2, "__version__", None),
            "reason": None if compatible else "cv2 is installed but lacks the OpenCV Haar APIs",
        }
    except ImportError:
        pass
    return result


def _mean_luma_right_strip(path: Path) -> tuple[float, float]:
    try:
        from PIL import Image, ImageStat
    except ImportError as exc:
        raise ToolError("Pillow is required for baseline frame analysis") from exc
    with Image.open(path) as image:
        gray = image.convert("L").resize((64, 36))
        left = gray.crop((0, 0, 48, 36))
        right = gray.crop((48, 0, 64, 36))
        return ImageStat.Stat(left).mean[0], ImageStat.Stat(right).mean[0]


def _mean_luma(path: Path) -> float:
    try:
        from PIL import Image, ImageStat
    except ImportError as exc:
        raise ToolError("Pillow is required for baseline frame analysis") from exc
    with Image.open(path) as image:
        gray = image.convert("L").resize((64, 36))
        return float(ImageStat.Stat(gray).mean[0])


def _frame_has_right_rail(path: Path) -> bool:
    left, right = _mean_luma_right_strip(path)
    # A per-frame signal only.  It must never become a whole-video layout
    # assumption because Meet can switch to full-screen or grid layouts.
    return right < 65 and left > 25 and right + 8 < left


def _active_layout_boxes(
    layout_regions: list[dict[str, Any]], timestamp: float
) -> list[tuple[float, float, float, float]]:
    return [
        tuple(region["box"])
        for region in layout_regions
        if region.get("kind") == "participant_panel"
        and float(region.get("start", 0.0)) <= timestamp < float(region.get("end", timestamp + 1e-6))
    ]


def _region_key(box: Iterable[float]) -> tuple[int, int, int, int]:
    return tuple(round(float(value) * 10000) for value in box)  # type: ignore[return-value]


def _merge_regions(regions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge adjacent observations while retaining their time boundaries."""
    ordered = sorted(
        (dict(region) for region in regions),
        key=lambda region: (
            _region_key(region.get("box", [0, 0, 0, 0])),
            float(region.get("start", 0.0)),
        ),
    )
    merged: list[dict[str, Any]] = []
    for region in ordered:
        if not merged:
            merged.append(region)
            continue
        previous = merged[-1]
        same_box = _region_key(previous.get("box", [])) == _region_key(region.get("box", []))
        same_kind = previous.get("kind") == region.get("kind")
        merge_gap = 4.25 if region.get("kind") == "participant_fullscreen" else 0.25
        touches = float(region.get("start", 0.0)) <= float(previous.get("end", 0.0)) + merge_gap
        if same_box and same_kind and touches:
            previous["end"] = max(float(previous.get("end", 0.0)), float(region.get("end", 0.0)))
            previous["confidence"] = max(
                float(previous.get("confidence", 0.0)), float(region.get("confidence", 0.0))
            )
            previous["sources"] = list(dict.fromkeys([*previous.get("sources", []), *region.get("sources", [])]))
        else:
            merged.append(region)
    # Face tracking can emit duplicate boxes for the same physical presenter
    # because different Haar cascades fire at slightly different positions.
    # Compact only those duplicates; genuinely moving boxes remain independent
    # regions so the renderer does not turn a face oval into a board-wide band.
    physical = sorted(
        (region for region in merged if region.get("kind") == "physical_face"),
        key=lambda region: float(region.get("start", 0.0)),
    )
    compact: list[dict[str, Any]] = []
    for region in physical:
        if not compact:
            compact.append(region)
            continue
        previous = compact[-1]
        first_box = [float(value) for value in previous.get("box", [0, 0, 0, 0])]
        second_box = [float(value) for value in region.get("box", [0, 0, 0, 0])]
        first_center = (first_box[0] + first_box[2] / 2.0, first_box[1] + first_box[3] / 2.0)
        second_center = (second_box[0] + second_box[2] / 2.0, second_box[1] + second_box[3] / 2.0)
        center_distance = ((first_center[0] - second_center[0]) ** 2 + (first_center[1] - second_center[1]) ** 2) ** 0.5
        close_in_time = float(region.get("start", 0.0)) <= float(previous.get("end", 0.0)) + 0.25
        # Do not collapse a moving presenter into one wide spatial envelope.
        # Only coalesce nearly identical boxes (usually duplicate Haar
        # cascades at one timestamp); motion remains a sequence of local
        # masks so the whiteboard and upper body stay visible.
        overlap = _face_iou(tuple(first_box), tuple(second_box))
        nearly_same_position = center_distance <= 0.025 and overlap >= 0.60
        if close_in_time and nearly_same_position:
            # Duplicate detector boxes are close enough to average.  Taking
            # their union here would slowly inflate a moving face back into a
            # large rectangle even though the regions are otherwise local.
            previous["box"] = [
                round((first_box[0] + second_box[0]) / 2.0, 6),
                round((first_box[1] + second_box[1]) / 2.0, 6),
                round((first_box[2] + second_box[2]) / 2.0, 6),
                round((first_box[3] + second_box[3]) / 2.0, 6),
            ]
            previous["end"] = max(float(previous.get("end", 0.0)), float(region.get("end", 0.0)))
            previous["confidence"] = max(
                float(previous.get("confidence", 0.0)), float(region.get("confidence", 0.0))
            )
            previous["sources"] = list(dict.fromkeys([
                *previous.get("sources", []), *region.get("sources", []),
            ]))
        else:
            compact.append(region)
    non_physical = [region for region in merged if region.get("kind") != "physical_face"]
    return sorted([*non_physical, *compact], key=lambda item: (
        float(item.get("start", 0.0)), item.get("kind", ""),
    ))


def _select_ocr_entries(
    entries: list[tuple[Path, float, float, float]],
    layout_regions: list[dict[str, Any]],
    known_names: list[str],
    limit: int = 180,
) -> list[tuple[Path, float, float, float]]:
    """Bound expensive OCR while retaining every non-rail layout transition."""
    candidates = [
        entry for entry in entries
        if not _active_layout_boxes(layout_regions, entry[1])
    ]
    if not candidates and entries:
        # Keep a small panel sample for detector evidence and compatibility
        # with short/rail-only clips; the enclosing panel itself is already
        # the rendered privacy region.
        candidates = entries[: min(20, len(entries))]
    if len(candidates) <= limit:
        return candidates
    # Uniform coverage plus explicit first/last frames protects short
    # full-screen scenes without turning a long recording into thousands of
    # serial Tesseract processes.
    indexes = {0, len(candidates) - 1}
    stride = (len(candidates) - 1) / max(limit - 2, 1)
    indexes.update(round(index * stride) for index in range(limit - 2))
    return [candidates[index] for index in sorted(indexes)]


def detect_layout_regions(
    frame_paths: Iterable[Path], duration: float, width: int, height: int
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Classify participant panels per sample, never as one global rail."""
    samples = list(frame_paths)
    warnings: list[str] = []
    evidence: dict[str, Any] = {
        "backend": "local-layout-v2",
        "sample_count": len(samples),
        "class": "unknown",
        "right_panel_ratio": 0.0,
        "protected_shared_content": None,
    }
    if not samples:
        return [], ["no frames were available for layout analysis"], evidence
    dark_hits = 0
    luma_samples: list[dict[str, float]] = []
    entries = _sample_entries(samples, duration)
    rail_regions: list[dict[str, Any]] = []
    for path, timestamp, start, end in entries:
        left, right = _mean_luma_right_strip(path)
        luma_samples.append({"left": round(left, 2), "right": round(right, 2)})
        if _frame_has_right_rail(path):
            dark_hits += 1
            rail_regions.append({
                "id": f"layout-right-participant-panel-{len(rail_regions):04d}",
                "start": start,
                "end": end,
                "kind": "participant_panel",
                "mode": "blur",
                "box": [0.75, 0.0, 0.25, 1.0],
                "confidence": 0.80,
                "sources": ["local-layout-v2", "right_panel_luma"],
                "review_required": True,
            })
    ratio = dark_hits / len(samples)
    evidence["right_panel_ratio"] = round(ratio, 3)
    evidence["luma_samples"] = luma_samples[:12]
    if not rail_regions:
        warnings.append(
            "layout model found no right rail; localized fullscreen/grid face and OCR detectors were used"
            if whiteboard_mode_enabled()
            else "layout model found no right rail; full-frame/grid detectors were used"
        )
    else:
        evidence["class"] = "temporal-participant-layouts"
        evidence["protected_shared_content"] = [0.0, 0.0, 0.75, 1.0]
    rail_regions = _merge_regions(rail_regions)
    evidence["temporal_region_count"] = len(rail_regions)
    return rail_regions, warnings, evidence


def _panel_boxes(layout_regions: list[dict[str, Any]]) -> list[tuple[float, float, float, float]]:
    return [tuple(item["box"]) for item in layout_regions if item.get("kind") == "participant_panel"]


def detect_ocr_regions(
    frame_paths: Iterable[Path],
    layout_regions: list[dict[str, Any]],
    known_names: Iterable[str] = (),
    duration: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """OCR participant labels in the active layout, including full-screen views.

    The detector intentionally samples the whole recording.  In a right-rail
    frame it reads the rail crop; in any other layout it reads the full frame
    and only accepts lower-third/edge text unless transcript names are known.
    """
    capability = detector_capabilities()["ocr"]
    evidence: dict[str, Any] = {
        "backend": capability.get("backend"),
        "available": capability.get("available"),
        "detections": 0,
    }
    warnings: list[str] = []
    if not capability.get("available"):
        evidence["reason"] = capability.get("reason")
        warnings.append(f"OCR unavailable: {capability.get('reason')}; visible names remain review-required")
        return [], evidence, warnings
    try:
        import pytesseract  # type: ignore
        from PIL import Image
        from pytesseract import Output
    except ImportError as exc:
        warnings.append(f"OCR unavailable: {exc}")
        evidence["available"] = False
        return [], evidence, warnings

    names = [name.casefold() for name in known_names]
    found: dict[tuple[int, ...], dict[str, Any]] = {}
    fullscreen_frames: dict[tuple[int, int], tuple[float, float]] = {}
    frames = list(frame_paths)
    effective_duration = duration or max((float(item.get("end", 0.0)) for item in layout_regions), default=0.0)
    effective_duration = effective_duration or float(len(frames) * 4.0)
    entries = _sample_entries(frames, effective_duration)
    # Older jobs do not carry a duration in layout regions.  Their fallback
    # entries are still useful; each frame is treated as one temporal segment.
    if not entries:
        entries = _sample_entries(frames, float(len(frames) * 4.0))
    for frame, timestamp, segment_start, segment_end in _select_ocr_entries(entries, layout_regions, names):
        with Image.open(frame) as image:
            image = image.convert("RGB")
            iw, ih = image.size
            panels = _active_layout_boxes(layout_regions, timestamp)
            crops = [(box, image.crop((
                int(box[0] * iw), int(box[1] * ih),
                int((box[0] + box[2]) * iw), int((box[1] + box[3]) * ih),
            ))) for box in panels]
            if not crops:
                crops = [((0.0, 0.0, 1.0, 1.0), image)]
            for (px, py, pw, ph), crop in crops:
                crop = image.crop((int(px * iw), int(py * ih), int((px + pw) * iw), int((py + ph) * ih)))
                data = pytesseract.image_to_data(crop, output_type=Output.DICT, config="--psm 11")
                for i, raw in enumerate(data.get("text", [])):
                    text = " ".join(str(raw).split()).strip()
                    if not text or not re.search(r"[A-Za-z]", text):
                        continue
                    confidence = float(data.get("conf", ["-1"])[i] or -1)
                    x, y, w, h = [int(data[key][i]) for key in ("left", "top", "width", "height")]
                    known_name = any(name in text.casefold() or text.casefold() in name for name in names)
                    # Full-frame OCR can see slide text.  Without transcript
                    # names, restrict automatic name candidates to the lower
                    # third/edge where Meet labels and nameplates appear.
                    relative_y = y / max(crop.height, 1)
                    participant_position = relative_y >= 0.62 or (px < 0.20 and relative_y >= 0.45)
                    if confidence < 35 and not known_name:
                        continue
                    if not panels and not known_name:
                        # Bright full-frame OCR is commonly slide/UI text, not
                        # a participant label. Transcript-provided names remain
                        # accepted anywhere, but still produce only this local
                        # OCR word box.
                        if not participant_position or _mean_luma(frame) >= 80:
                            continue
                    box = (
                        round(px + (x / max(crop.width, 1)) * pw, 6),
                        round(py + (y / max(crop.height, 1)) * ph, 6),
                        round((w / max(crop.width, 1)) * pw, 6),
                        round((h / max(crop.height, 1)) * ph, 6),
                    )
                    key = tuple(round(value * 10000) for value in box) + (round(segment_start * 100),)
                    found[key] = {
                        "id": f"ocr-name-{len(found):04d}",
                        "start": segment_start,
                        "end": segment_end,
                        "kind": "participant_name",
                        "mode": "blur",
                        "box": list(box),
                        "text": text,
                        "confidence": round(max(0.0, min(1.0, confidence / 100.0)), 3),
                        "sources": ["tesseract-ocr", "participant-panel-layout" if panels else "full-frame-layout"],
                        "review_required": True,
                    }
                    if not whiteboard_mode_enabled() and not panels and (known_name or _mean_luma(frame) < 80):
                        fullscreen_frames[(round(segment_start * 100), round(segment_end * 100))] = (
                            segment_start, segment_end
                        )
    if not whiteboard_mode_enabled():
        for segment_start, segment_end in fullscreen_frames.values():
            key = (0, 0, 10000, 10000, round(segment_start * 100))
            found[key] = {
                "id": f"participant-fullscreen-{len(found):04d}",
                "start": segment_start,
                "end": segment_end,
                "kind": "participant_fullscreen",
                "mode": "blur",
                "box": [0.0, 0.0, 1.0, 1.0],
                "confidence": 0.68,
                "sources": ["tesseract-ocr", "full-frame-layout"],
                "review_required": True,
            }
    results = _merge_regions(found.values())
    evidence["detections"] = len(results)
    if not results:
        warnings.append("OCR backend ran but found no confident participant-panel text")
    return results, evidence, warnings


def _face_iou(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
    """Return intersection-over-union for normalized face boxes."""
    ax1, ay1, aw, ah = first
    bx1, by1, bw, bh = second
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


def _localized_face_box(
    box: tuple[float, float, float, float],
    *, pad_x_factor: float = 0.25,
    pad_y_factor: float = 0.35,
    min_pad_x: float = 0.02,
    min_pad_y: float = 0.03,
    min_width: float = 0.0,
    min_height: float = 0.0,
    max_width: float = 0.85,
    max_height: float = 0.85,
) -> tuple[float, float, float, float]:
    """Expand a detected face without ever turning it into a full-frame mask."""
    x, y, width, height = (float(value) for value in box)
    center_x, center_y = x + width / 2.0, y + height / 2.0
    expanded_width = min(
        max_width,
        max(min_width, width + 2.0 * max(width * pad_x_factor, min_pad_x)),
    )
    expanded_height = min(
        max_height,
        max(min_height, height + 2.0 * max(height * pad_y_factor, min_pad_y)),
    )
    # When the detected envelope touches a frame edge, preserve that edge
    # after applying the cap.  Re-centering a capped box inward can leave the
    # outer part of a presenter unmasked precisely during edge motion.
    if x <= 0.01:
        left = 0.0
    elif x + width >= 0.99:
        left = 1.0 - expanded_width
    else:
        left = min(max(center_x - expanded_width / 2.0, 0.0), 1.0 - expanded_width)
    if y <= 0.01:
        top = 0.0
    elif y + height >= 0.99:
        top = 1.0 - expanded_height
    else:
        top = min(max(center_y - expanded_height / 2.0, 0.0), 1.0 - expanded_height)
    return (
        round(left, 6), round(top, 6),
        round(expanded_width, 6), round(expanded_height, 6),
    )


# Physical-camera/whiteboard faces need a wider motion envelope than small
# Meet tiles. These are intentionally below a full-frame mask, while large
# enough to retain the prior rectangular path's edge coverage. The minimum is
# important: a single small Haar box during a transition must not collapse the
# privacy mask to a tiny oval around only the eyes/nose.
PHYSICAL_FACE_MIN_WIDTH = 0.55
PHYSICAL_FACE_MIN_HEIGHT = 0.50
PHYSICAL_FACE_MAX_WIDTH = 0.75
PHYSICAL_FACE_MAX_HEIGHT = 0.65


def _face_cascade_specs(cv2: Any, *, panel: bool) -> list[tuple[str, Path, bool, int]]:
    """Describe the cascades used for a panel or arbitrary full-frame scene.

    The participant rail remains on the fast frontal path.  Unknown/grid/
    camera scenes additionally use profile and alternate frontal cascades, and
    mirror profile detections so either side of a presenter is covered.
    """
    root = Path(cv2.data.haarcascades)
    specs = [("frontal", root / "haarcascade_frontalface_default.xml", False, 5)]
    if not panel:
        specs.extend([
            ("frontal-alt2", root / "haarcascade_frontalface_alt2.xml", False, 4),
            ("profile", root / "haarcascade_profileface.xml", False, 4),
            ("profile-mirror", root / "haarcascade_profileface.xml", True, 4),
        ])
    return specs


def _face_tracks(observations: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Cluster detections across time so unstable slide false positives drop out."""
    tracks: list[list[dict[str, Any]]] = []
    for observation in sorted(observations, key=lambda item: float(item["timestamp"])):
        box = tuple(float(value) for value in observation["box"])
        center = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)
        best: list[dict[str, Any]] | None = None
        best_score = 0.0
        for track in tracks:
            last = track[-1]
            if bool(last["panel"]) != bool(observation["panel"]):
                continue
            timestamp_gap = float(observation["timestamp"]) - float(last["timestamp"])
            if timestamp_gap > 8.0:
                continue
            last_box = tuple(float(value) for value in last["box"])
            last_center = (last_box[0] + last_box[2] / 2.0, last_box[1] + last_box[3] / 2.0)
            distance = ((center[0] - last_center[0]) ** 2 + (center[1] - last_center[1]) ** 2) ** 0.5
            overlap = _face_iou(box, last_box)
            # A participant rail is already an enclosing blur region and can
            # contain several independently moving tiles. Do not merge
            # separate rail faces merely because they are nearby.
            # The same rule applies to grid/camera proposals detected in the
            # same frame: two people can be close without being one track.
            if overlap < 0.08 and (bool(observation["panel"]) or timestamp_gap <= 0.01):
                continue
            # Cap motion tolerance so a large false proposal cannot bridge a
            # genuine face track to an unrelated board mark at the edge.  A
            # physical-camera presenter can move by more than one face width
            # between 4-fps samples, though; using only a 0.18-frame cap can
            # split that person into tracks and leave a render gap.  Allow a
            # modest, size-aware corridor for non-panel scenes.  Same-frame
            # proposals are still kept separate above, and the temporal
            # support/bright-scene filters below reject isolated board marks.
            proximity = max(
                0.10,
                min(0.32, 0.08 + 1.8 * max(box[2], box[3], last_box[2], last_box[3])),
            )
            # Do not let an alternate-frontal false positive near the top of
            # a bright whiteboard join a real face lower in the frame merely
            # because the horizontal motion corridor is wide enough.  Real
            # presenter motion can be fast horizontally, but a quarter-frame
            # vertical jump between adjacent dense samples is not a plausible
            # track continuation.
            vertical_proximity = max(
                0.14, 1.2 * max(box[3], last_box[3])
            )
            if overlap < 0.08 and abs(center[1] - last_center[1]) > vertical_proximity:
                continue
            if overlap < 0.08 and distance > proximity:
                continue
            score = overlap + max(0.0, 1.0 - distance / max(proximity, 1e-6)) * 0.25
            if score > best_score:
                best, best_score = track, score
        if best is None:
            tracks.append([observation])
        else:
            best.append(observation)
    return tracks


def _median_face_box(track: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    values = [tuple(float(value) for value in item["box"]) for item in track]
    return tuple(sorted(component)[len(component) // 2] for component in zip(*values))  # type: ignore[return-value]


def _union_face_boxes(
    boxes: Iterable[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    values = list(boxes)
    if not values:
        return 0.0, 0.0, 0.0, 0.0
    left = min(box[0] for box in values)
    top = min(box[1] for box in values)
    right = max(box[0] + box[2] for box in values)
    bottom = max(box[1] + box[3] for box in values)
    return left, top, max(0.0, right - left), max(0.0, bottom - top)


def _face_observation_groups(
    track: list[dict[str, Any]], duration: float,
) -> list[tuple[float, float, tuple[float, float, float, float]]]:
    """Turn detections into short intervals with motion-safe union boxes."""
    by_timestamp: dict[float, list[dict[str, Any]]] = {}
    for item in track:
        by_timestamp.setdefault(round(float(item["timestamp"]), 3), []).append(item)
    observations = [
        (timestamp, items)
        for timestamp, items in sorted(by_timestamp.items())
    ]
    groups: list[tuple[float, float, tuple[float, float, float, float]]] = []
    for index, (timestamp, items) in enumerate(observations):
        current_boxes = [tuple(float(value) for value in item["box"]) for item in items]
        current = _union_face_boxes(current_boxes)
        next_start = (
            min(float(item["segment_start"]) for item in observations[index + 1][1])
            if index + 1 < len(observations)
            else max(float(item["segment_end"]) for item in items)
        )
        start = min(float(item["segment_start"]) for item in items)
        # A face can be missed in the first decoded frame (profile/lighting
        # transitions are common). Protect the leading safety window rather
        # than exposing a short unblurred prefix before the first proposal.
        if index == 0 and start <= dense_face_fallback_seconds():
            start = 0.0
        end = min(float(duration), max(start, next_start))
        # Union adjacent detections so a moving face cannot leave the blur
        # between two triggered frames. Add a small safety margin around the
        # union, then clamp to normalized frame coordinates.
        boxes_for_interval = [current]
        if index + 1 < len(observations):
            boxes_for_interval.append(_union_face_boxes(
                tuple(float(value) for value in item["box"])
                for item in observations[index + 1][1]
            ))
        left, top, width, height = _union_face_boxes(boxes_for_interval)
        # Keep temporal association/safety bounded independently of the user
        # face-padding setting.  Applying a very large requested padding here
        # can inflate a valid face interval past the bright-scene false-
        # proposal guard and discard the interval entirely.  The user setting
        # is applied later by _localized_face_box, where it controls the final
        # rendered mask without changing detector classification.
        motion_padding = 0.12
        pad_x = max(0.01, width * motion_padding)
        pad_y = max(0.015, height * motion_padding)
        left = max(0.0, left - pad_x)
        top = max(0.0, top - pad_y)
        right = min(1.0, left + width + 2.0 * pad_x)
        bottom = min(1.0, top + height + 2.0 * pad_y)
        groups.append((round(start, 3), round(end, 3), (
            round(left, 6), round(top, 6), round(right - left, 6), round(bottom - top, 6)
        )))
    return [group for group in groups if group[1] > group[0]]


def _detect_face_regions_legacy(
    frame_paths: Iterable[Path], layout_regions: list[dict[str, Any]], duration: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Stable meeting detector retained for the default non-whiteboard path."""
    capability = detector_capabilities()["face"]
    evidence: dict[str, Any] = {
        "backend": capability.get("backend"),
        "available": capability.get("available"),
        "detections": 0,
    }
    warnings: list[str] = []
    if not capability.get("available"):
        evidence["reason"] = capability.get("reason")
        warnings.append(f"face detection unavailable: {capability.get('reason')}; participant panel fallback remains active")
        return [], evidence, warnings
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        warnings.append(f"face detection unavailable: {exc}")
        evidence["available"] = False
        return [], evidence, warnings
    required = ("CascadeClassifier", "imread", "cvtColor", "data")
    if not all(hasattr(cv2, name) for name in required) or not hasattr(cv2.data, "haarcascades"):
        reason = "installed cv2 lacks CascadeClassifier/imread/cvtColor/data"
        evidence["available"] = False
        evidence["reason"] = reason
        warnings.append(f"face detection unavailable: {reason}; participant panel fallback remains active")
        return [], evidence, warnings
    cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    if cascade.empty():
        warnings.append("OpenCV Haar face cascade could not be loaded")
        evidence["available"] = False
        return [], evidence, warnings
    found: dict[tuple[int, ...], dict[str, Any]] = {}
    for frame, timestamp, segment_start, segment_end in _sample_entries(frame_paths, duration):
        image = cv2.imread(str(frame))
        if image is None:
            continue
        ih, iw = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        panels = _active_layout_boxes(layout_regions, timestamp)
        scan_boxes = panels or [(0.0, 0.0, 1.0, 1.0)]
        for px, py, pw, ph in scan_boxes:
            x0, y0, x1, y1 = int(px * iw), int(py * ih), int((px + pw) * iw), int((py + ph) * ih)
            roi = gray[y0:y1, x0:x1]
            faces = cascade.detectMultiScale(roi, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
            for x, y, w, h in faces:
                pad_x, pad_y = int(w * 0.18), int(h * 0.22)
                bx, by = max(0, x0 + x - pad_x), max(0, y0 + y - pad_y)
                bw, bh = min(iw - bx, w + 2 * pad_x), min(ih - by, h + 2 * pad_y)
                box = (round(bx / iw, 6), round(by / ih, 6), round(bw / iw, 6), round(bh / ih, 6))
                fullscreen = not panels and (
                    box[2] >= 0.16 or box[3] >= 0.24 or box[2] * box[3] >= 0.045
                )
                if fullscreen:
                    render_box, kind = (0.0, 0.0, 1.0, 1.0), "participant_fullscreen"
                elif not panels:
                    pad_x = max(box[2] * 1.25, 0.045)
                    pad_y = max(box[3] * 1.50, 0.075)
                    bx2, by2 = max(0.0, box[0] - pad_x), max(0.0, box[1] - pad_y)
                    ex, ey = min(1.0, box[0] + box[2] + pad_x), min(1.0, box[1] + box[3] + pad_y)
                    render_box, kind = (
                        round(bx2, 6), round(by2, 6), round(ex - bx2, 6), round(ey - by2, 6)
                    ), "participant_tile"
                else:
                    render_box, kind = box, "participant_face"
                key = tuple(round(value * 10000) for value in render_box) + (round(segment_start * 100),)
                found[key] = {
                    "id": f"face-{len(found):04d}",
                    "start": segment_start,
                    "end": segment_end,
                    "kind": kind,
                    "mode": "blur",
                    "box": list(render_box),
                    "confidence": 0.70,
                    "sources": ["opencv-haar", "participant-panel-layout" if panels else "full-frame-layout"],
                    "review_required": True,
                }
    results = _merge_regions(found.values())
    evidence["detections"] = len(results)
    if not results:
        warnings.append("face detector ran but found no stable participant faces; avatars still rely on panel fallback")
    return results, evidence, warnings


def detect_face_regions(
    frame_paths: Iterable[Path], layout_regions: list[dict[str, Any]], duration: float,
    face_frame_paths: Iterable[Path] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    if not whiteboard_mode_enabled():
        return _detect_face_regions_legacy(frame_paths, layout_regions, duration)
    return _detect_face_regions_whiteboard(frame_paths, layout_regions, duration, face_frame_paths)


def _detect_face_regions_whiteboard(
    frame_paths: Iterable[Path], layout_regions: list[dict[str, Any]], duration: float,
    face_frame_paths: Iterable[Path] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Detect faces in rails, grids, and arbitrary full-frame camera scenes.

    Layout detection only narrows the search to a known participant rail.  In
    every other layout the complete frame is searched, regardless of aspect
    ratio.  Profile proposals are stabilized across samples before rendering;
    this prevents a single whiteboard stroke or slide element from becoming a
    blur region.
    """
    capability = detector_capabilities()["face"]
    evidence: dict[str, Any] = {
        "backend": capability.get("backend"),
        "available": capability.get("available"),
        "detections": 0,
        "raw_detections": 0,
        "stable_tracks": 0,
        "cascades": [],
    }
    warnings: list[str] = []
    if not capability.get("available"):
        evidence["reason"] = capability.get("reason")
        warnings.append(f"face detection unavailable: {capability.get('reason')}; participant panel fallback remains active")
        return [], evidence, warnings
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        warnings.append(f"face detection unavailable: {exc}")
        evidence["available"] = False
        return [], evidence, warnings
    required = ("CascadeClassifier", "imread", "cvtColor", "data")
    if not all(hasattr(cv2, name) for name in required) or not hasattr(cv2.data, "haarcascades"):
        reason = "installed cv2 lacks CascadeClassifier/imread/cvtColor/data"
        evidence["available"] = False
        evidence["reason"] = reason
        warnings.append(f"face detection unavailable: {reason}; participant panel fallback remains active")
        return [], evidence, warnings

    observations: list[dict[str, Any]] = []
    loaded_cascades: dict[str, tuple[Any, bool, int]] = {}
    # The normal samples are retained as the fallback for callers and older
    # jobs.  New jobs provide a separate low-resolution 2-fps stream so face
    # coverage is dense for long recordings as well as short mobile clips.
    # Layout/OCR sampling remains intentionally sparse and is not used as a
    # proxy for face coverage.
    dense_paths = list(face_frame_paths or [])
    entries = _sample_entries(dense_paths, duration) if dense_paths else _sample_entries(frame_paths, duration)
    evidence["sampling"] = {
        "source": "dense-face-frames" if dense_paths else "analysis-frames",
        "frame_count": len(entries),
        "fps": dense_face_sampling_fps() if dense_paths else None,
        "selection": "frame-diff+periodic-fallback" if dense_paths else "fixed-analysis-samples",
    }
    for frame, timestamp, segment_start, segment_end in entries:
        image = cv2.imread(str(frame))
        if image is None:
            continue
        ih, iw = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        # Large phone videos are cheaper to scan after a bounded resize. Face
        # boxes are normalized, so this does not change output coordinates.
        scale = 1.0
        max_dimension = max(gray.shape[:2])
        if max_dimension > 1600 and hasattr(cv2, "resize"):
            scale = 1600.0 / max_dimension
            gray = cv2.resize(gray, (max(1, int(gray.shape[1] * scale)), max(1, int(gray.shape[0] * scale))))
        dh, dw = gray.shape[:2]
        panels = _active_layout_boxes(layout_regions, timestamp)
        # The enclosing participant rail is already a complete blur region.
        # Avoid spending four cascade passes on every low-resolution rail frame
        # and, more importantly, avoid turning rail faces into independent
        # regions that could obscure shared content at a boundary.
        if dense_paths and panels:
            continue
        # A known rail/grid tile is searched locally; all other aspect ratios
        # and fullscreen camera views are searched over the complete frame.
        scan_boxes = panels or [(0.0, 0.0, 1.0, 1.0)]
        for px, py, pw, ph in scan_boxes:
            x0, y0 = int(px * dw), int(py * dh)
            x1, y1 = int((px + pw) * dw), int((py + ph) * dh)
            roi = gray[y0:y1, x0:x1]
            panel = bool(panels)
            for detector_name, cascade_path, mirrored, neighbors in _face_cascade_specs(cv2, panel=panel):
                if detector_name not in loaded_cascades:
                    try:
                        cascade = cv2.CascadeClassifier(str(cascade_path))
                    except Exception as exc:
                        _verbose(f"face cascade {detector_name} failed to load: {exc}")
                        continue
                    if cascade.empty():
                        continue
                    loaded_cascades[detector_name] = (cascade, mirrored, neighbors)
                    evidence["cascades"].append(detector_name)
                cascade, mirrored, neighbors = loaded_cascades[detector_name]
                variants: list[tuple[str, Any]] = [("gray", roi)]
                if detector_name.startswith("profile") and hasattr(cv2, "equalizeHist"):
                    variants.append(("equalized", cv2.equalizeHist(roi)))
                for variant_name, variant in variants:
                    if mirrored:
                        if not hasattr(cv2, "flip"):
                            continue
                        variant = cv2.flip(variant, 1)
                    try:
                        faces = cascade.detectMultiScale(
                            variant,
                            scaleFactor=1.1,
                            minNeighbors=neighbors,
                            minSize=(max(16, int(24 * scale)), max(16, int(24 * scale))),
                        )
                    except Exception as exc:
                        _verbose(f"face cascade {detector_name} failed on {frame.name}: {exc}")
                        continue
                    for x, y, w, h in faces:
                        if mirrored:
                            x = variant.shape[1] - int(x) - int(w)
                        pad_x, pad_y = int(w * 0.18), int(h * 0.22)
                        bx = max(0, int(x0 + x - pad_x))
                        by = max(0, int(y0 + y - pad_y))
                        # Keep padding inside the active scan box. In a
                        # participant rail, allowing it to spill into the
                        # shared canvas defeats panel coalescing and can add
                        # redundant filters outside the protected rail.
                        bw = min(x1 - bx, int(w + 2 * pad_x))
                        bh = min(y1 - by, int(h + 2 * pad_y))
                        box = (
                            round(bx / max(dw, 1), 6), round(by / max(dh, 1), 6),
                            round(bw / max(dw, 1), 6), round(bh / max(dh, 1), 6),
                        )
                        observations.append({
                            "timestamp": timestamp,
                            "segment_start": segment_start,
                            "segment_end": segment_end,
                            "box": box,
                            "panel": panel,
                            "detector": detector_name,
                            "variant": variant_name,
                            "luma": float(gray.mean()) if hasattr(gray, "mean") else None,
                            "raw_width": float(w) / max(dw, 1),
                            "raw_height": float(h) / max(dh, 1),
                            "raw_y": float(y0 + y) / max(dh, 1),
                        })
    evidence["raw_detections"] = len(observations)
    if not observations:
        warnings.append("face detector ran but found no participant-face proposals; review remains required")
        return [], evidence, warnings

    tracks = _face_tracks(observations)
    results: list[dict[str, Any]] = []
    rejected_tracks = 0
    for track in tracks:
        support = len({round(float(item["timestamp"]), 3) for item in track})
        detectors = {str(item["detector"]) for item in track}
        panel = bool(track[0]["panel"])
        luma_values = [float(item["luma"]) for item in track if item.get("luma") is not None]
        luma = sum(luma_values) / len(luma_values) if luma_values else 0.0
        # Profile-only proposals need temporal support because whiteboard
        # strokes can resemble a profile at one instant. Frontal proposals are
        # retained even when a clip has only one usable sample for privacy.
        if not panel and support < 2 and detectors and all(name.startswith("profile") for name in detectors) and len(entries) > 1:
            continue
        if not panel:
            max_raw_width = max(float(item.get("raw_width", 1.0)) for item in track)
            min_raw_y = min(float(item.get("raw_y", 0.0)) for item in track)
            has_profile = any(name.startswith("profile") for name in detectors)
            # Bright camera/whiteboard scenes produce occasional enormous Haar
            # proposals from board borders, writing, or a person's torso. A
            # real face in this mode should be a localized, temporally stable
            # proposal; reject these before they become a giant local blur.
            if luma >= 90.0 and (
                max_raw_width >= 0.45
                or max(float(item.get("raw_height", 1.0)) for item in track) >= 0.45
                or support < 2
            ):
                rejected_tracks += 1
                continue
            # On bright boards, the alternate-frontal cascade often locks on
            # to a writing stroke or a frame edge near the top of the image.
            # A real presenter face in this scene has profile/frontal support
            # lower in the frame; reject these isolated top-edge proposals.
            if luma >= 90.0 and not has_profile and max_raw_width < 0.18 and min_raw_y < 0.14:
                rejected_tracks += 1
                continue
            # Small isolated proposals at the bottom edge or on a bright
            # canvas are usually board marks/controls. Stable small faces in a
            # grid remain eligible, as do single-sample clips.
            if len(entries) > 1 and support < 2 and max_raw_width < 0.07:
                rejected_tracks += 1
                continue
            if max_raw_width < 0.07 and min_raw_y > 0.80:
                rejected_tracks += 1
                continue
            # Alternate frontal cascades are useful as supporting evidence but
            # are especially prone to interpreting handwriting as a face. A
            # small non-profile track must persist longer than a profile-backed
            # track or have a genuinely large face proposal.
            if len(entries) > 1 and max_raw_width < 0.15 and (support < 3 or (not has_profile and support < 4)):
                rejected_tracks += 1
                continue
        sources = [f"opencv-haar-{name}" for name in sorted(detectors)]
        sources.append("participant-panel-layout" if panel else "layout-independent-face")
        temporal_groups = _face_observation_groups(track, duration)
        for group_index, (start, end, box) in enumerate(temporal_groups):
            # A giant physical-scene proposal is usually a whiteboard/body
            # contour rather than a face. Keep shared content visible and
            # rely on the smaller stable face tracks instead.
            if luma >= 90.0 and (box[2] >= 0.70 or box[3] >= 0.60):
                continue
            if panel:
                render_box, kind = box, "participant_face"
            elif luma >= 90.0:
                # Bright physical-camera/whiteboard scenes should keep the
                # board visible; blur the moving face region instead of the
                # entire frame. Haar boxes often cover only the eye/nose
                # portion of a profile, so add a larger safety margin around
                # the complete head/face. The temporal group already unions
                # the current and next observations, so do not union the
                # entire track here: a presenter moving across the frame
                # would otherwise blur a large strip of the whiteboard for
                # the whole clip. This is deliberately conservative around
                # the detected head, while keeping the surrounding board
                # visible.
                render_box, kind = _localized_face_box(
                    box,
                    pad_x_factor=configured_face_padding(),
                    pad_y_factor=configured_face_padding(),
                    min_pad_x=0.012,
                    min_pad_y=0.020,
                    min_width=PHYSICAL_FACE_MIN_WIDTH,
                    min_height=PHYSICAL_FACE_MIN_HEIGHT,
                    # Keep the physical-face mask localized, but leave enough
                    # horizontal safety envelope for a presenter at the frame
                    # edge. The previous 0.34 x 0.38 cap clipped the face in
                    # the whiteboard fixture (the old rectangular path used
                    # about 0.55 frame width). A feathered ellipse still
                    # protects the center while the larger crop prevents the
                    # nose/hair from falling outside the mask during motion.
                    max_width=PHYSICAL_FACE_MAX_WIDTH,
                    max_height=PHYSICAL_FACE_MAX_HEIGHT,
                ), "physical_face"
            elif box[2] >= 0.16 or box[3] >= 0.24 or box[2] * box[3] >= 0.045:
                # Preserve the established dark/UI fullscreen-presenter path.
                # Bright physical-camera/whiteboard scenes never take this
                # branch; they remain localized to the detected face.
                render_box, kind = (0.0, 0.0, 1.0, 1.0), "participant_fullscreen"
            else:
                # Grids and arbitrary camera layouts use a bounded local face
                # box. A large bright-scene proposal was handled above as a
                # physical face; only dark UI fullscreen proposals are allowed
                # to use the established full-view mask.
                render_box, kind = _localized_face_box(box), "participant_tile"
            results.append({
                "id": f"face-{len(results):04d}-{group_index:03d}",
                "start": start,
                "end": end,
                "kind": kind,
                "mode": "blur",
                "box": list(render_box),
                "confidence": round(min(0.95, 0.62 + 0.05 * min(support, 6)), 3),
                "sources": [*sources, "frame-diff-triggered"],
                "support_samples": support,
                "review_required": True,
            })
    results = _merge_regions(results)
    evidence["stable_tracks"] = max(0, len(tracks) - rejected_tracks)
    evidence["temporal_regions"] = len(results)
    evidence["rejected_tracks"] = rejected_tracks
    evidence["detections"] = len(results)
    if not results:
        warnings.append("face proposals were unstable; no face blur was emitted and manual review is required")
    return results, evidence, warnings


def detect_visual_regions(
    frame_paths: Iterable[Path], duration: float, width: int, height: int,
    known_names: Iterable[str] = (),
    face_frame_paths: Iterable[Path] | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Run layout, OCR, and face detectors and return auditable evidence."""
    frames = list(frame_paths)
    layout, warnings, layout_evidence = detect_layout_regions(frames, duration, width, height)
    # Retain the established dark/UI fullscreen-presenter protection. This is
    # distinct from the removed unknown-layout fallback: bright physical
    # camera/whiteboard scenes do not enter this path.
    dark_fullscreen: list[dict[str, Any]] = []
    for frame, timestamp, segment_start, segment_end in _sample_entries(frames, duration):
        if not _active_layout_boxes(layout, timestamp) and _mean_luma(frame) < 80:
            dark_fullscreen.append({
                "id": f"layout-dark-fullscreen-{len(dark_fullscreen):04d}",
                "start": segment_start,
                "end": segment_end,
                "kind": "participant_fullscreen",
                "mode": "blur",
                "box": [0.0, 0.0, 1.0, 1.0],
                "confidence": 0.62,
                "sources": ["local-layout-v2", "dark-full-frame"],
                "review_required": True,
            })
    # OCR and face detection are independent and can run concurrently.  The
    # detector libraries may create their own native threads, so keep this
    # fan-out bounded rather than creating one thread per frame.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="priv-vision") as pool:
        ocr_future = pool.submit(detect_ocr_regions, frames, layout, known_names, duration)
        face_future = pool.submit(
            detect_face_regions,
            frames,
            layout,
            duration,
            face_frame_paths,
        )
        try:
            ocr, ocr_evidence, ocr_warnings = ocr_future.result()
        except Exception as exc:  # optional backend failures must not abort sanitization
            ocr, ocr_evidence, ocr_warnings = [], {
                "backend": "pytesseract", "available": False, "detections": 0,
                "reason": f"backend error: {exc}",
            }, [f"OCR backend error: {exc}; visible names remain review-required"]
        try:
            faces, face_evidence, face_warnings = face_future.result()
        except Exception as exc:  # optional backend failures must not abort sanitization
            faces, face_evidence, face_warnings = [], {
                "backend": "opencv-haar", "available": False, "detections": 0,
                "reason": f"backend error: {exc}",
            }, [f"face backend error: {exc}; participant panel fallback remains active"]
    candidates = [*layout, *dark_fullscreen, *ocr, *faces]
    # Keep participant-rail, established dark fullscreen-presenter, and local
    # face/name regions. Unknown bright/physical scenes without local evidence
    # remain review-required instead of receiving a blanket fallback blur.
    regions = _coalesce_render_regions(candidates)
    if not regions and duration > 0:
        warnings.append("no localized participant face/name region was detected; whole-frame masking is disabled and manual review is required")
    detectors = {
        "layout": layout_evidence,
        "dark_fullscreen_regions": len(dark_fullscreen),
        "aggressive_fallback_regions": 0,
        "aggressive_redaction": aggressive_redaction_enabled(),
        "ocr": ocr_evidence,
        "face": face_evidence,
        "candidate_region_count": len(candidates),
        "render_region_count": len(regions),
        "ocr_regions": ocr,
        "face_regions": faces,
    }
    return regions, [*warnings, *ocr_warnings, *face_warnings], detectors


def _coalesce_render_regions(regions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate adjacent detections and drop boxes enclosed by a panel."""
    merged = _merge_regions(regions)
    result: list[dict[str, Any]] = []
    for region in merged:
        box = [float(value) for value in region.get("box", [0, 0, 0, 0])]
        contained = False
        for outer in merged:
            if outer is region:
                continue
            outer_box = [float(value) for value in outer.get("box", [0, 0, 0, 0])]
            same_window = (
                float(outer.get("start", 0.0)) <= float(region.get("start", 0.0)) + 0.25
                and float(outer.get("end", 0.0)) >= float(region.get("end", 0.0)) - 0.25
            )
            encloses = (
                outer_box[0] <= box[0] and outer_box[1] <= box[1]
                and outer_box[0] + outer_box[2] >= box[0] + box[2]
                and outer_box[1] + outer_box[3] >= box[1] + box[3]
                and (outer_box[2] * outer_box[3]) > (box[2] * box[3]) * 1.15
            )
            if same_window and encloses and outer.get("kind") in {
                "participant_panel", "participant_fullscreen", "participant_tile"
            }:
                contained = True
                break
        if not contained:
            result.append(region)
    return sorted(result, key=lambda item: (float(item.get("start", 0.0)), item.get("kind", "")))


def detect_speech_intervals(video: Path, duration: float) -> tuple[list[dict[str, Any]], list[str]]:
    """Analyze speech activity for evidence only; never use it as name redaction."""
    if duration <= 0:
        return [], ["video duration is unknown; speech activity was not analyzed"]
    try:
        result = run([
            _tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(video),
            "-af", "silencedetect=noise=-35dB:d=0.30", "-f", "null", "-",
        ], progress_stage="analysing audio", duration=duration)
    except ToolError as exc:
        return [], [f"speech activity analysis unavailable: {exc}"]
    text = result.stderr or ""
    silence_start: float | None = None
    silence_ranges: list[tuple[float, float]] = []
    for line in text.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        end_match = re.search(r"silence_end:\s*([0-9.]+)", line)
        if start_match:
            silence_start = float(start_match.group(1))
        elif end_match and silence_start is not None:
            silence_ranges.append((silence_start, min(duration, float(end_match.group(1)))))
            silence_start = None
    if silence_start is not None:
        silence_ranges.append((silence_start, duration))
    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in silence_ranges:
        if start - cursor >= 0.25:
            speech.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= 0.25:
        speech.append((cursor, duration))
    if not silence_ranges:
        speech = [(0.0, duration)]
    return [{"start": round(start, 3), "end": round(end, 3)} for start, end in speech], []


def audio_levels(video: Path) -> dict[str, float | None]:
    """Measure audio without modifying it, used to catch accidental audio loss."""
    try:
        result = run([
            _tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(video),
            "-af", "volumedetect", "-f", "null", "-",
        ], progress_stage="checking audio", duration=None)
    except ToolError:
        return {"mean_db": None, "max_db": None}
    text = result.stderr or ""
    mean = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", text)
    maximum = re.search(r"max_volume:\s*(-?[0-9.]+) dB", text)
    return {
        "mean_db": float(mean.group(1)) if mean else None,
        "max_db": float(maximum.group(1)) if maximum else None,
    }


def _enable(interval: dict[str, Any]) -> str:
    return f"between(t,{float(interval['start']):.3f},{float(interval['end']):.3f})"


def _blur_filter_graph_legacy(visual: list[dict[str, Any]]) -> tuple[str, str]:
    """Stable meeting render graph from the pre-whiteboard path."""
    current = "0:v"
    filters: list[str] = []
    for index, item in enumerate(visual):
        x, y, w, h = [float(value) for value in item["box"]]
        enable = _enable(item)
        mode = item.get("mode", "blur")
        if mode == "solid":
            next_label = f"v{index}"
            filters.append(
                f"[{current}]drawbox=x=iw*{x}:y=ih*{y}:w=iw*{w}:h=ih*{h}:color=black@1:t=fill:enable='{enable}'[{next_label}]"
            )
            current = next_label
            continue
        base, roi, blurred, next_label = (f"b{index}", f"r{index}", f"z{index}", f"v{index}")
        filters.append(f"[{current}]split=2[{base}][{roi}]")
        filters.append(
            f"[{roi}]crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y},"
            f"boxblur=luma_radius=18:luma_power=2:chroma_radius=0:chroma_power=0:enable='{enable}'[{blurred}]"
        )
        filters.append(
            f"[{base}][{blurred}]overlay=x=main_w*{x}:y=main_h*{y}:enable='{enable}'[{next_label}]"
        )
        current = next_label
    return ";".join(filters) + (f";[{current}]null[vout]" if filters else "[0:v]null[vout]"), "[vout]"


def _blur_filter_graph(
    visual: list[dict[str, Any]], *, blur_radius: int | None = None,
    blur_opacity: float | None = None,
    frame_width: int | None = None,
    frame_height: int | None = None,
) -> tuple[str, str]:
    if not whiteboard_mode_enabled():
        return _blur_filter_graph_legacy(visual)
    return _blur_filter_graph_whiteboard(
        visual,
        blur_radius=blur_radius,
        blur_opacity=blur_opacity,
        frame_width=frame_width,
        frame_height=frame_height,
    )


def _blur_filter_graph_whiteboard(
    visual: list[dict[str, Any]], *, blur_radius: int | None = None,
    blur_opacity: float | None = None,
    frame_width: int | None = None,
    frame_height: int | None = None,
) -> tuple[str, str]:
    radius = configured_blur_radius() if blur_radius is None else max(1, min(128, int(blur_radius)))
    opacity = configured_blur_opacity() if blur_opacity is None else max(0.0, min(1.0, float(blur_opacity)))
    current = "0:v"
    filters: list[str] = []
    for index, item in enumerate(visual):
        x, y, w, h = [float(value) for value in item["box"]]
        enable = _enable(item)
        mode = item.get("mode", "blur")
        if mode == "solid":
            next_label = f"v{index}"
            filters.append(
                f"[{current}]drawbox=x=iw*{x}:y=ih*{y}:w=iw*{w}:h=ih*{h}:color=black@1:t=fill:enable='{enable}'[{next_label}]"
            )
            current = next_label
            continue
        base, roi, blurred, next_label = (f"b{index}", f"r{index}", f"z{index}", f"v{index}")
        filters.append(f"[{current}]split=2[{base}][{roi}]")
        # FFmpeg rejects a radius larger than half of a small crop.  Keep the
        # user-requested strength for normal/full-frame regions while bounding
        # it by the actual cropped dimensions so --blur-radius 128 cannot
        # break tiny participant name/face tiles.
        safe_radius = f"min({radius}\\,min(w\\,h)/2-1)"
        alpha = ""
        overlay_blurred = blurred
        if item.get("kind") == "physical_face":
            # Physical-camera/whiteboard presenters use a soft ellipse rather
            # than the rectangular crop used for participant rails and
            # full-screen presenter views.  The feathered edge avoids a hard
            # visible boundary while keeping the board and upper body outside
            # the face mask.  Escape expression commas for FFmpeg's filter
            # parser; the opacity multiplier is applied inside the alpha mask
            # so 100% remains fully opaque at the ellipse centre.
            ellipse = (
                "((X-W/2)*(X-W/2)/(W*W/4)+"
                "(Y-H/2)*(Y-H/2)/(H*H/4))"
            )
            feather_start = 0.60
            masked_blurred = f"e{index}"
            mask = f"a{index}"
            alpha_expr = (
                f"if(lte({ellipse}\\,{feather_start})\\,{255.0 * opacity:.6f}\\,"
                f"if(gte({ellipse}\\,1)\\,0\\,"
                f"{255.0 * opacity:.6f}*(1-{ellipse})/(1-{feather_start})))"
            )
            original = f"o{index}"
            to_blur = f"p{index}"
            if frame_width and frame_height:
                # The crop dimensions are normalized in the manifest. Render
                # the small mask directly at the same rounded pixel size as
                # the crop; this avoids scale2ref's aspect-ratio rounding
                # producing a one-pixel mismatch on portrait videos.
                # FFmpeg rounds crop dimensions and then makes them even for
                # the normal yuv420p path. Match that behavior for the mask.
                crop_width = max(2, int(float(frame_width) * w + 0.5) // 2 * 2)
                crop_height = max(2, int(float(frame_height) * h + 0.5) // 2 * 2)
                # Process the physical face at quarter resolution. Upscaling
                # the already blurred/elliptically masked crop preserves the
                # privacy effect while avoiding a full-resolution maskedmerge
                # over a large 4K rectangle on every frame.
                work_width = max(2, (crop_width // 4) // 2 * 2)
                work_height = max(2, (crop_height // 4) // 2 * 2)
                work_radius = max(1, radius // 4)
                work_safe_radius = f"min({work_radius}\\,min(w\\,h)/2-1)"
                mask_resize = f"scale=w={work_width}:h={work_height}:flags=bilinear"
                restore_size = f"scale=w={crop_width}:h={crop_height}:flags=bilinear"
                filters.append(
                    f"[{roi}]crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y},"
                    f"scale=w={work_width}:h={work_height}:flags=bilinear,"
                    f"split=2[{original}][{to_blur}];"
                    f"[{to_blur}]boxblur=luma_radius={work_safe_radius}:luma_power=2:"
                    f"chroma_radius=0:chroma_power=0:enable='{enable}'[{blurred}]"
                )
                # Bound the synthetic mask to the source interval.  An
                # unbounded color source can keep maskedmerge producing
                # frames after the real input reaches EOF, which appears as
                # a render hang on short/high-resolution clips.
                mask_duration = max(float(item.get("end", 0.0)), 0.01)
                filters.append(
                    f"color=black:s=64x64:r=25:d={mask_duration:.3f},format=gray,"
                    f"geq=lum='{alpha_expr}',gblur=sigma=3:steps=2[m{index}];"
                    f"[m{index}]{mask_resize}[{mask}];"
                    f"[{original}][{blurred}][{mask}]maskedmerge[u{index}];"
                    f"[u{index}]{restore_size}[{masked_blurred}]"
                )
            else:
                # Unit-test/legacy callers may not provide source dimensions;
                # retain a dynamic reference-based fallback for them.
                filters.append(
                    f"[{roi}]crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y},"
                    f"split=2[{original}][{to_blur}];"
                    f"[{to_blur}]boxblur=luma_radius={safe_radius}:luma_power=2:"
                    f"chroma_radius=0:chroma_power=0:enable='{enable}'[{blurred}]"
                )
                mask_duration = max(float(item.get("end", 0.0)), 0.01)
                filters.append(
                    f"color=black:s=64x64:r=25:d={mask_duration:.3f},format=gray,"
                    f"geq=lum='{alpha_expr}',gblur=sigma=3:steps=2[m{index}];"
                    f"[m{index}][{blurred}]scale2ref=w=oh*dar:h=ih[{mask}][s{index}];"
                    f"[{original}][s{index}][{mask}]maskedmerge[{masked_blurred}]"
                )
            overlay_blurred = masked_blurred
            filters.append(
                f"[{base}][{overlay_blurred}]overlay=x=main_w*{x}:y=main_h*{y}:enable='{enable}'[{next_label}]"
            )
            current = next_label
            continue
        elif opacity < 1.0:
            # Lower opacity intentionally reveals some original detail. It is
            # provided for review/creative workflows; privacy-max runs should
            # keep the default 1.0.
            # The source crop is normally YUV without alpha. Convert to RGBA
            # before scaling alpha; otherwise FFmpeg treats the mixer input as
            # transparent and every partial-opacity setting behaves alike.
            alpha = f",format=rgba,colorchannelmixer=aa={opacity:.6f}"
        filters.append(
            f"[{roi}]crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y},"
            # Small face crops can reject the default chroma radius. Luma
            # blur is sufficient to destroy facial detail and works for both
            # the full participant rail and small detected face boxes.
            f"boxblur=luma_radius={safe_radius}:luma_power=2:chroma_radius=0:chroma_power=0:enable='{enable}'{alpha}[{blurred}]"
        )
        filters.append(
            f"[{base}][{overlay_blurred}]overlay=x=main_w*{x}:y=main_h*{y}:enable='{enable}'[{next_label}]"
        )
        current = next_label
    return ";".join(filters) + (f";[{current}]null[vout]" if filters else "[0:v]null[vout]"), "[vout]"


def render(video: Path, manifest: dict[str, Any], output: Path) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    # Encode into a same-directory temporary path and publish atomically only
    # after FFmpeg completes. Ctrl+C or an encoding failure can therefore not
    # leave a truncated file at the user-facing output path.
    partial_output = output.with_name(f".{output.stem}.{os.getpid()}.part{output.suffix}")
    try:
        partial_output.unlink()
    except FileNotFoundError:
        pass
    visual = manifest.get("visual_redactions", [])
    audio = manifest.get("audio_redactions", [])
    stored_radius = manifest.get("privacy", {}).get("blur_radius")
    stored_opacity = manifest.get("privacy", {}).get("blur_opacity")
    stored_face_padding = manifest.get("privacy", {}).get("face_padding")
    blur_radius = configured_blur_radius()
    blur_opacity = configured_blur_opacity()
    face_padding = configured_face_padding()
    if "PRIV_SANITIZER_BLUR_RADIUS" not in os.environ and stored_radius is not None:
        try:
            blur_radius = max(1, min(128, int(stored_radius)))
        except (TypeError, ValueError):
            blur_radius = configured_blur_radius()
    if "PRIV_SANITIZER_BLUR_OPACITY" not in os.environ and stored_opacity is not None:
        try:
            blur_opacity = max(0.0, min(1.0, float(stored_opacity)))
        except (TypeError, ValueError):
            blur_opacity = configured_blur_opacity()
    if "PRIV_SANITIZER_FACE_PADDING" not in os.environ and stored_face_padding is not None:
        try:
            face_padding = max(0.0, min(0.5, float(stored_face_padding)))
        except (TypeError, ValueError):
            face_padding = configured_face_padding()
    settings = effective_privacy_settings()
    if not whiteboard_mode_enabled():
        # Preserve the stable meeting renderer's original blur settings unless
        # the experimental whiteboard path is explicitly selected.
        blur_radius = settings["blur_radius"]
        blur_opacity = settings["blur_opacity"]
        face_padding = settings["face_padding"]
    args = [_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
    if visual:
        if whiteboard_mode_enabled():
            graph, output_label = _blur_filter_graph(
                visual, blur_radius=blur_radius, blur_opacity=blur_opacity,
                frame_width=int(manifest.get("video", {}).get("width") or 0) or None,
                frame_height=int(manifest.get("video", {}).get("height") or 0) or None,
            )
        else:
            graph, output_label = _blur_filter_graph(visual)
        args += ["-filter_complex", graph, "-map", output_label]
    else:
        args += ["-map", "0:v:0"]
    audio_filters: list[str] = []
    if manifest.get("audio_mode") in {"mute-names", "mute-speaker-intervals"}:
        for item in audio:
            audio_filters.append(f"volume=volume=0:enable='{_enable(item)}'")
    if audio_filters:
        args += ["-af", ",".join(audio_filters)]
    # The default is tuned for the normal 8-core workstation/container. Both
    # settings remain overridable for constrained or shared machines.
    ffmpeg_threads = os.environ.get("PRIV_SANITIZER_FFMPEG_THREADS", "8")
    x264_preset = os.environ.get("PRIV_SANITIZER_X264_PRESET", "ultrafast")
    _status(f"render settings: ffmpeg_threads={ffmpeg_threads}, x264_preset={x264_preset}")
    args += [
        "-map", "0:a:0?", "-c:v", "libx264", "-preset", x264_preset,
        "-crf", "22", "-pix_fmt", "yuv420p", "-threads", ffmpeg_threads,
        "-filter_threads", ffmpeg_threads, "-filter_complex_threads", ffmpeg_threads,
    ]
    if manifest.get("audio_mode") == "preserve" and not audio_filters:
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-b:a", "128k"]
    args += ["-map_metadata", "-1", "-movflags", "+faststart", str(partial_output)]
    render_started = time.perf_counter()
    try:
        run(
            args,
            progress_stage="rendering video",
            duration=float(manifest.get("video", {}).get("duration") or 0),
        )
        os.replace(partial_output, output)
    except BaseException:
        try:
            partial_output.unlink()
        except FileNotFoundError:
            pass
        raise
    return {
        "output": str(output),
        "visual_filters": len(visual),
        "audio_filters": len(audio_filters),
        "audio_mode": manifest.get("audio_mode"),
        "blur_radius": blur_radius,
        "blur_opacity": blur_opacity,
        "face_padding": face_padding,
        "ffmpeg_threads": ffmpeg_threads,
        "x264_preset": x264_preset,
        "render_seconds": round(time.perf_counter() - render_started, 3),
    }
