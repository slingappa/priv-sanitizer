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
    return (
        os.environ.get("PRIV_SANITIZER_QUIET") != "1"
        and os.environ.get("PRIV_SANITIZER_VERBOSE", "").lower() in {"1", "true", "yes", "on"}
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
        print(f"[priv-sanitizer] {rendered}", file=os.sys.stderr, flush=True)
        _verbose_log(rendered)


def _heartbeat_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("PRIV_SANITIZER_HEARTBEAT_SECONDS", "15")))
    except ValueError:
        return 15.0


def _progress_status(stage: str, percent: int, *, final: bool = False) -> None:
    if os.environ.get("PRIV_SANITIZER_QUIET") == "1":
        return
    message = f"[priv-sanitizer] {stage}: {percent:3d}%"
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
        heartbeat = _heartbeat_seconds()
        while selector.get_map():
            events = selector.select(timeout=heartbeat)
            if not events:
                if process.poll() is None:
                    elapsed = time.perf_counter() - started
                    quiet_for = time.perf_counter() - last_progress_at
                    _verbose(
                        f"{progress_stage} still running after {elapsed:.0f}s "
                        f"(no FFmpeg progress for {quiet_for:.0f}s; last reported {last_percent if last_percent >= 0 else 'none'}%)"
                    )
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
    pattern = output_dir / "frame-%06d.jpg"
    run([
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-vf", f"fps=1/{max(every, 0.25):.3f}",
        "-q:v", "5", str(pattern),
    ], progress_stage="sampling frames", duration=duration)
    return sorted(output_dir.glob("frame-*.jpg"))


def detector_capabilities() -> dict[str, Any]:
    """Report optional backend availability explicitly."""
    result: dict[str, Any] = {
        "layout": {"available": True, "backend": "local-layout-v1"},
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


def detect_layout_regions(
    frame_paths: Iterable[Path], duration: float, width: int, height: int
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Classify common Meet layouts with a deterministic local layout model."""
    samples = list(frame_paths)
    warnings: list[str] = []
    evidence: dict[str, Any] = {
        "backend": "local-layout-v1",
        "sample_count": len(samples),
        "class": "unknown",
        "right_panel_ratio": 0.0,
        "protected_shared_content": None,
    }
    if not samples:
        return [], ["no frames were available for layout analysis"], evidence
    dark_hits = 0
    luma_samples: list[dict[str, float]] = []
    for path in samples:
        left, right = _mean_luma_right_strip(path)
        luma_samples.append({"left": round(left, 2), "right": round(right, 2)})
        if right < 65 and left > 25 and right + 8 < left:
            dark_hits += 1
    ratio = dark_hits / len(samples)
    evidence["right_panel_ratio"] = round(ratio, 3)
    evidence["luma_samples"] = luma_samples[:12]
    if ratio < 0.35:
        warnings.append("layout model found no stable participant rail; full-frame/grid layouts need review")
        return [], warnings, evidence
    evidence["class"] = "shared-content-plus-right-participant-rail"
    evidence["protected_shared_content"] = [0.0, 0.0, 0.75, 1.0]
    return [{
        "id": "layout-right-participant-panel",
        "start": 0.0,
        "end": round(duration, 3),
        "kind": "participant_panel",
        "mode": "blur",
        "box": [0.75, 0.0, 0.25, 1.0],
        "confidence": round(min(0.95, 0.60 + ratio * 0.35), 3),
        "sources": ["local-layout-v1", "right_panel_luma"],
        "review_required": True,
    }], warnings, evidence


def _panel_boxes(layout_regions: list[dict[str, Any]]) -> list[tuple[float, float, float, float]]:
    return [tuple(item["box"]) for item in layout_regions if item.get("kind") == "participant_panel"]


def detect_ocr_regions(
    frame_paths: Iterable[Path],
    layout_regions: list[dict[str, Any]],
    known_names: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """OCR likely participant panels with Tesseract when locally available."""
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
    found: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    panels = _panel_boxes(layout_regions)
    for frame in list(frame_paths)[:80]:
        with Image.open(frame) as image:
            image = image.convert("RGB")
            iw, ih = image.size
            for px, py, pw, ph in panels:
                crop = image.crop((int(px * iw), int(py * ih), int((px + pw) * iw), int((py + ph) * ih)))
                data = pytesseract.image_to_data(crop, output_type=Output.DICT, config="--psm 11")
                for i, raw in enumerate(data.get("text", [])):
                    text = " ".join(str(raw).split()).strip()
                    if not text or not re.search(r"[A-Za-z]", text):
                        continue
                    confidence = float(data.get("conf", ["-1"])[i] or -1)
                    if confidence < 35 and not any(text.casefold() in name for name in names):
                        continue
                    x, y, w, h = [int(data[key][i]) for key in ("left", "top", "width", "height")]
                    box = (
                        round(px + (x / max(crop.width, 1)) * pw, 6),
                        round(py + (y / max(crop.height, 1)) * ph, 6),
                        round((w / max(crop.width, 1)) * pw, 6),
                        round((h / max(crop.height, 1)) * ph, 6),
                    )
                    key = tuple(round(value * 10000) for value in box)
                    found[key] = {
                        "id": f"ocr-name-{len(found):04d}",
                        "start": 0.0,
                        "end": None,
                        "kind": "participant_name",
                        "mode": "blur",
                        "box": list(box),
                        "text": text,
                        "confidence": round(max(0.0, min(1.0, confidence / 100.0)), 3),
                        "sources": ["tesseract-ocr", "participant-panel-layout"],
                        "review_required": True,
                    }
    results = list(found.values())
    for item in results:
        item["end"] = max((region["end"] for region in layout_regions), default=0.0)
    evidence["detections"] = len(results)
    if not results:
        warnings.append("OCR backend ran but found no confident participant-panel text")
    return results, evidence, warnings


def detect_face_regions(
    frame_paths: Iterable[Path], layout_regions: list[dict[str, Any]], duration: float
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Detect faces in participant panels with OpenCV's local Haar detector."""
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
    found: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    panels = _panel_boxes(layout_regions)
    for frame in list(frame_paths)[:80]:
        image = cv2.imread(str(frame))
        if image is None:
            continue
        ih, iw = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        for px, py, pw, ph in panels:
            x0, y0, x1, y1 = int(px * iw), int(py * ih), int((px + pw) * iw), int((py + ph) * ih)
            roi = gray[y0:y1, x0:x1]
            faces = cascade.detectMultiScale(roi, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
            for x, y, w, h in faces:
                pad_x, pad_y = int(w * 0.18), int(h * 0.22)
                bx, by = max(0, x0 + x - pad_x), max(0, y0 + y - pad_y)
                bw, bh = min(iw - bx, w + 2 * pad_x), min(ih - by, h + 2 * pad_y)
                box = (round(bx / iw, 6), round(by / ih, 6), round(bw / iw, 6), round(bh / ih, 6))
                key = tuple(round(value * 10000) for value in box)
                found[key] = {
                    "id": f"face-{len(found):04d}",
                    "start": 0.0,
                    "end": round(duration, 3),
                    "kind": "participant_face",
                    "mode": "blur",
                    "box": list(box),
                    "confidence": 0.70,
                    "sources": ["opencv-haar", "participant-panel-layout"],
                    "review_required": True,
                }
    results = list(found.values())
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
    # OCR and face detection are independent and can run concurrently.  The
    # detector libraries may create their own native threads, so keep this
    # fan-out bounded rather than creating one thread per frame.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="priv-vision") as pool:
        ocr_future = pool.submit(detect_ocr_regions, frames, layout, known_names)
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
    candidates = [*layout, *ocr, *faces]
    # A stable participant rail already encloses the OCR and face candidates.
    # Rendering all nested boxes creates a long serial FFmpeg graph and gives
    # no additional privacy benefit. Keep the detailed candidates in the
    # analysis evidence, but render the enclosing rail only. If no enclosing
    # layout is known, fall back to the precise OCR/face boxes.
    regions = layout if layout else [*ocr, *faces]
    detectors = {
        "layout": layout_evidence,
        "ocr": ocr_evidence,
        "face": face_evidence,
        "candidate_region_count": len(candidates),
        "render_region_count": len(regions),
        "ocr_regions": ocr,
        "face_regions": faces,
    }
    return regions, [*warnings, *ocr_warnings, *face_warnings], detectors


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
            f"boxblur=luma_radius=18:luma_power=2:chroma_radius=0:chroma_power=0[{blurred}]"
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
