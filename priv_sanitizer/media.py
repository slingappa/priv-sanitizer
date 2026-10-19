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
        except subprocess.TimeoutExpired:
            while True:
                elapsed = time.perf_counter() - started
                _verbose(f"command still running after {elapsed:.0f}s: {shlex.join(args)}")
                try:
                    stdout, stderr = process.communicate(timeout=_heartbeat_seconds())
                    break
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
    return merged


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
        warnings.append("layout model found no right rail; full-frame/grid detectors were used")
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
                        # a participant label.  Dark speaker/avatar scenes are
                        # covered by the conservative full-frame fallback;
                        # transcript-provided names remain accepted anywhere.
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
                    if not panels and (known_name or _mean_luma(frame) < 80):
                        fullscreen_frames[(round(segment_start * 100), round(segment_end * 100))] = (
                            segment_start, segment_end
                        )
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


def detect_face_regions(
    frame_paths: Iterable[Path], layout_regions: list[dict[str, Any]], duration: float
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Detect faces in active panels or anywhere in non-rail layouts."""
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
        # When no panel is active, inspect the full frame.  This is what
        # catches a speaker view or centered avatar after a shared-screen
        # segment.  In a known rail layout, restrict face search to the rail
        # so faces on shared slides remain untouched.
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
                # A large centered face is a speaker/full-screen view.  Blur
                # the complete participant view so the nameplate/avatar and
                # surrounding identifying UI are covered as well.
                fullscreen = (
                    not panels and (box[2] >= 0.16 or box[3] >= 0.24 or box[2] * box[3] >= 0.045)
                )
                if fullscreen:
                    render_box = (0.0, 0.0, 1.0, 1.0)
                    kind = "participant_fullscreen"
                elif not panels:
                    # Grid/tile layouts can place a participant anywhere and
                    # usually draw the name directly below the face.  Expand
                    # the face crop to cover that local tile without masking
                    # the entire shared canvas.
                    pad_x = max(box[2] * 1.25, 0.045)
                    pad_y = max(box[3] * 1.50, 0.075)
                    bx2 = max(0.0, box[0] - pad_x)
                    by2 = max(0.0, box[1] - pad_y)
                    ex = min(1.0, box[0] + box[2] + pad_x)
                    ey = min(1.0, box[1] + box[3] + pad_y)
                    render_box = (round(bx2, 6), round(by2, 6), round(ex - bx2, 6), round(ey - by2, 6))
                    kind = "participant_tile"
                else:
                    render_box = box
                    kind = "participant_face"
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


def detect_visual_regions(
    frame_paths: Iterable[Path], duration: float, width: int, height: int,
    known_names: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Run layout, OCR, and face detectors and return auditable evidence."""
    frames = list(frame_paths)
    layout, warnings, layout_evidence = detect_layout_regions(frames, duration, width, height)
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
        face_future = pool.submit(detect_face_regions, frames, layout, duration)
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
    # Layout regions are now temporal.  Keep precise OCR/face/full-screen
    # regions as well, but suppress nested face/name boxes while an enclosing
    # participant rail/full-screen region covers the same interval.  This
    # avoids a giant filter graph without discarding detections from other
    # layouts later in the recording.
    regions = _coalesce_render_regions(candidates)
    detectors = {
        "layout": layout_evidence,
        "dark_fullscreen_regions": len(dark_fullscreen),
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


def _blur_filter_graph(visual: list[dict[str, Any]]) -> tuple[str, str]:
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
            # Small face crops can reject the default chroma radius. Luma
            # blur is sufficient to destroy facial detail and works for both
            # the full participant rail and small detected face boxes.
            f"boxblur=luma_radius=18:luma_power=2:chroma_radius=0:chroma_power=0:enable='{enable}'[{blurred}]"
        )
        filters.append(
            f"[{base}][{blurred}]overlay=x=main_w*{x}:y=main_h*{y}:enable='{enable}'[{next_label}]"
        )
        current = next_label
    return ";".join(filters) + (f";[{current}]null[vout]" if filters else "[0:v]null[vout]"), "[vout]"


def render(video: Path, manifest: dict[str, Any], output: Path) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    visual = manifest.get("visual_redactions", [])
    audio = manifest.get("audio_redactions", [])
    args = [_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
    if visual:
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
    args += ["-map_metadata", "-1", "-movflags", "+faststart", str(output)]
    render_started = time.perf_counter()
    run(
        args,
        progress_stage="rendering video",
        duration=float(manifest.get("video", {}).get("duration") or 0),
    )
    return {
        "output": str(output),
        "visual_filters": len(visual),
        "audio_filters": len(audio_filters),
        "audio_mode": manifest.get("audio_mode"),
        "ffmpeg_threads": ffmpeg_threads,
        "x264_preset": x264_preset,
        "render_seconds": round(time.perf_counter() - render_started, 3),
    }
