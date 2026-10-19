"""Command-line interface for priv-sanitizer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .media import ToolError, probe
from .pipeline import batch, baseline, harden, read_json, render_job, verify_output, write_json


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        raise argparse.ArgumentTypeError(f"path does not exist: {path}")
    return path


def _directory(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.exists() or not path.is_dir():
        raise argparse.ArgumentTypeError(f"directory does not exist: {path}")
    return path


def _add_verbose_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="enable detailed diagnostics (written to verbose.log)",
    )


def _add_quiet_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
        help="suppress status/progress diagnostics and verbose.log for this run",
    )


def _positive_jobs(value: str) -> int:
    try:
        jobs = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("jobs must be a positive integer") from exc
    if jobs < 1:
        raise argparse.ArgumentTypeError("jobs must be a positive integer")
    return jobs


def _add_jobs_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-j", "--jobs", type=_positive_jobs, default=argparse.SUPPRESS,
        help="number of FFmpeg/filter threads (default: 8)",
    )


def _add_keep_artifacts_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-k", "--keep-artifacts", "--keep-videos", dest="keep_artifacts",
        action="store_true", default=argparse.SUPPRESS,
        help="keep generated audit artifacts for debugging (alias: --keep-videos)",
    )


def _add_whiteboard_option(parser: argparse.ArgumentParser, *, global_parser: bool = False) -> None:
    parser.add_argument(
        "-w", "--whiteboard", dest="whiteboard", action="store_true",
        default=False if global_parser else argparse.SUPPRESS,
        help="EXPERIMENTAL: enable physical-camera/whiteboard face detection and masking",
    )


def _blur_radius(value: str) -> int:
    try:
        radius = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("blur radius must be an integer from 1 to 128") from exc
    if not 1 <= radius <= 128:
        raise argparse.ArgumentTypeError("blur radius must be an integer from 1 to 128")
    return radius


def _blur_opacity(value: str) -> float:
    """Parse the fraction of the blurred layer placed over the source.

    Both a fraction (``0.75``) and a percentage (``75%``) are accepted so
    the setting is convenient in shell scripts and easy to read interactively.
    """
    text = value.strip()
    try:
        if text.endswith("%"):
            opacity = float(text[:-1]) / 100.0
        else:
            opacity = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "blur opacity must be a number from 0 to 1 (or 0% to 100%)"
        ) from exc
    if not 0.0 <= opacity <= 1.0:
        raise argparse.ArgumentTypeError(
            "blur opacity must be a number from 0 to 1 (or 0% to 100%)"
        )
    return opacity


def _face_padding(value: str) -> float:
    """Parse symmetric physical-face expansion as a fraction or percentage."""
    text = value.strip()
    try:
        padding = float(text[:-1]) / 100.0 if text.endswith("%") else float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "face padding must be a number from 0 to 0.5 (or 0% to 50%)"
        ) from exc
    if not 0.0 <= padding <= 0.5:
        raise argparse.ArgumentTypeError(
            "face padding must be a number from 0 to 0.5 (or 0% to 50%)"
        )
    return padding


def _add_privacy_options(parser: argparse.ArgumentParser, *, global_parser: bool = False) -> None:
    parser.add_argument(
        "--blur-radius", type=_blur_radius,
        default=None if global_parser else argparse.SUPPRESS,
        help="EXPERIMENTAL -w: FFmpeg blur radius in pixels (default: 64; higher is stronger)",
    )
    parser.add_argument(
        "--blur-opacity", type=_blur_opacity,
        default=None if global_parser else argparse.SUPPRESS,
        help="EXPERIMENTAL -w: blurred-layer opacity, 0–1 or 0%%–100%% (default: 1.0)",
    )
    parser.add_argument(
        "--face-padding", type=_face_padding,
        default=None if global_parser else argparse.SUPPRESS,
        help="EXPERIMENTAL -w: symmetric physical-face expansion on x/y, 0–0.5 or 0%%–50%% (default: 16%%)",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--aggressive", dest="aggressive", action="store_true",
        default=None if global_parser else argparse.SUPPRESS,
        help="EXPERIMENTAL -w: deprecated compatibility flag; whole-frame masking is disabled",
    )
    modes.add_argument(
        "--balanced", dest="aggressive", action="store_false",
        default=None if global_parser else argparse.SUPPRESS,
        help="EXPERIMENTAL -w: use localized participant face/name regions",
    )


def build_parser() -> argparse.ArgumentParser:
    examples = """Examples:
  # Batch every video in a folder; finals go to outputs_sanitized/.
  VIDEO_DIR="$PWD"
  cd "$VIDEO_DIR" && priv-sanitizer

  # The equivalent explicit batch command.
  priv-sanitizer batch --input "$VIDEO_DIR" \\
      --output "$VIDEO_DIR/outputs_sanitized" --artifacts "$VIDEO_DIR/artifacts"

  # Sanitize one video into a chosen audit/output directory.
  priv-sanitizer sanitize --video "$VIDEO_DIR/input.mp4" \\
      --output "$VIDEO_DIR/artifacts/input-single"

  # Override the default 8-thread/ultrafast render settings.
  PRIV_SANITIZER_FFMPEG_THREADS=4 \\
  PRIV_SANITIZER_X264_PRESET=veryfast \\
  priv-sanitizer batch --input "$VIDEO_DIR"

  # Opt in to experimental transcript-based audio name muting.
  priv-sanitizer batch --input "$VIDEO_DIR" --experimental-audio

  # Keep generated audit artifacts for debugging as well as final delivery copies.
  priv-sanitizer batch --input "$VIDEO_DIR" --keep-artifacts

  # Experimental physical-camera/whiteboard face/name blur.
  priv-sanitizer -w --blur-radius 96 --blur-opacity 100% --face-padding 20% -j8 -k
"""
    parser = argparse.ArgumentParser(
        prog="priv-sanitizer",
        description="Sanitize meeting video locally",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="enable detailed diagnostics (written to verbose.log)",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="suppress status/progress diagnostics and verbose.log for this run",
    )
    parser.add_argument(
        "-j", "--jobs", type=_positive_jobs, default=None,
        help="number of FFmpeg/filter threads (default: 8)",
    )
    _add_privacy_options(parser, global_parser=True)
    _add_whiteboard_option(parser, global_parser=True)
    _add_keep_artifacts_option(parser)
    sub = parser.add_subparsers(dest="command", required=False)
    parser.set_defaults(
        command="batch",
        input=Path.cwd(),
        output=Path.cwd() / "outputs_sanitized",
        artifacts=None,
        experimental_audio=False,
        keep_artifacts=False,
        whiteboard=False,
        every=4.0,
    )

    batch_cmd = sub.add_parser(
        "batch",
        help="sanitize every video in a folder with optional transcript hardening",
        description="Run baseline sanitization for every video, then optionally harden with matching transcripts.",
        epilog="Example:\n  priv-sanitizer batch --input . --output ./outputs_sanitized --artifacts ./artifacts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_verbose_option(batch_cmd)
    _add_quiet_option(batch_cmd)
    _add_jobs_option(batch_cmd)
    _add_keep_artifacts_option(batch_cmd)
    _add_privacy_options(batch_cmd)
    _add_whiteboard_option(batch_cmd)
    batch_cmd.add_argument("--input", type=_directory, default=Path.cwd())
    batch_cmd.add_argument("--output", type=Path, default=Path.cwd() / "outputs_sanitized")
    batch_cmd.add_argument(
        "--artifacts", type=Path, default=None,
        help="audit directory (default: sibling ./artifacts directory)",
    )
    batch_cmd.add_argument(
        "--experimental-audio", "--experimental-audio-redaction",
        action="store_true", dest="experimental_audio", default=False,
        help="EXPERIMENTAL: mute transcript-matched spoken-name windows",
    )
    batch_cmd.add_argument("--every", type=float, default=4.0)

    inspect = sub.add_parser("inspect", help="probe a video and initialize a job")
    _add_verbose_option(inspect)
    _add_quiet_option(inspect)
    _add_jobs_option(inspect)
    inspect.add_argument("video", type=_path)
    inspect.add_argument("--job", type=Path, required=True)

    analyze = sub.add_parser("analyze", help="run baseline video-only analysis")
    _add_verbose_option(analyze)
    _add_quiet_option(analyze)
    _add_jobs_option(analyze)
    _add_privacy_options(analyze)
    _add_whiteboard_option(analyze)
    analyze.add_argument("job", type=Path)
    analyze.add_argument("--pass", dest="analysis_pass", choices=["baseline"], default="baseline")
    analyze.add_argument("--audio-mode", choices=["preserve", "mute-names"], default="preserve")
    analyze.add_argument("--every", type=float, default=4.0)

    sanitize = sub.add_parser("sanitize", help="run baseline analysis and render")
    _add_verbose_option(sanitize)
    _add_quiet_option(sanitize)
    _add_jobs_option(sanitize)
    _add_privacy_options(sanitize)
    _add_whiteboard_option(sanitize)
    sanitize.add_argument("--video", type=_path, required=True)
    sanitize.add_argument("--output", type=Path, required=True)
    sanitize.add_argument("--audio-mode", choices=["preserve", "mute-names"], default="preserve")
    sanitize.add_argument("--every", type=float, default=4.0)

    harden_cmd = sub.add_parser("harden", help="apply optional transcript-assisted hardening")
    _add_verbose_option(harden_cmd)
    _add_quiet_option(harden_cmd)
    _add_jobs_option(harden_cmd)
    _add_privacy_options(harden_cmd)
    _add_whiteboard_option(harden_cmd)
    harden_cmd.add_argument("job", type=Path, help="baseline job directory")
    harden_cmd.add_argument("--transcript", type=_path, required=True)
    harden_cmd.add_argument("--job", dest="output_job", type=Path, required=True)
    harden_cmd.add_argument(
        "--transcript-offset", type=float, default=0.0,
        help="seconds to subtract from transcript timestamps (needed for extracted clips)",
    )
    harden_cmd.add_argument(
        "--experimental-audio", "--experimental-audio-redaction",
        action="store_true", dest="narrow_audio", default=False,
        help="EXPERIMENTAL: mute transcript-matched spoken-name windows",
    )
    harden_cmd.add_argument(
        "--no-audio-redaction", action="store_false", dest="narrow_audio",
        help=argparse.SUPPRESS,
    )

    render = sub.add_parser("render", help="render a job manifest")
    _add_verbose_option(render)
    _add_quiet_option(render)
    _add_jobs_option(render)
    _add_privacy_options(render)
    _add_whiteboard_option(render)
    render.add_argument("job", type=Path)
    render.add_argument("--output", type=Path, required=True)

    report = sub.add_parser("report", help="print a job report")
    _add_verbose_option(report)
    _add_quiet_option(report)
    _add_jobs_option(report)
    report.add_argument("job", type=Path)

    verify = sub.add_parser("verify", help="verify a rendered output")
    _add_verbose_option(verify)
    _add_quiet_option(verify)
    _add_jobs_option(verify)
    verify.add_argument("job", type=Path)
    verify.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.verbose:
        os.environ["PRIV_SANITIZER_VERBOSE"] = "1"
    if args.quiet:
        os.environ["PRIV_SANITIZER_QUIET"] = "1"
        os.environ["PRIV_SANITIZER_VERBOSE"] = "0"
    if args.jobs is not None:
        os.environ["PRIV_SANITIZER_FFMPEG_THREADS"] = str(args.jobs)
    if args.whiteboard:
        os.environ["PRIV_SANITIZER_WHITEBOARD"] = "1"
    if args.blur_radius is not None:
        os.environ["PRIV_SANITIZER_BLUR_RADIUS"] = str(args.blur_radius)
    if args.blur_opacity is not None:
        os.environ["PRIV_SANITIZER_BLUR_OPACITY"] = str(args.blur_opacity)
    if args.face_padding is not None:
        os.environ["PRIV_SANITIZER_FACE_PADDING"] = str(args.face_padding)
    if args.aggressive is not None:
        os.environ["PRIV_SANITIZER_AGGRESSIVE"] = "1" if args.aggressive else "0"
    try:
        if args.command == "batch":
            print(json.dumps(
                batch(
                    args.input,
                    args.output,
                    artifacts_dir=args.artifacts,
                    experimental_audio=args.experimental_audio,
                    keep_artifacts=args.keep_artifacts,
                    every=args.every,
                ),
                indent=2,
            ))
        elif args.command == "inspect":
            args.job.mkdir(parents=True, exist_ok=True)
            write_json(args.job / "probe.json", probe(args.video))
            print(json.dumps(read_json(args.job / "probe.json"), indent=2))
        elif args.command == "analyze":
            baseline(Path(read_json(args.job / "probe.json")["path"]), args.job, audio_mode=args.audio_mode, every=args.every)
            print(f"baseline analysis written to {args.job}")
        elif args.command == "sanitize":
            job = args.output / "job"
            baseline(args.video, job, audio_mode=args.audio_mode, every=args.every)
            result = render_job(job, args.output)
            print(json.dumps(result, indent=2))
        elif args.command == "harden":
            harden(
                args.job, args.transcript, args.output_job,
                narrow_audio=args.narrow_audio, transcript_offset=args.transcript_offset,
            )
            print(f"hardened analysis written to {args.output_job}")
        elif args.command == "render":
            print(json.dumps(render_job(args.job, args.output), indent=2))
        elif args.command == "report":
            print((args.job / "report.txt").read_text(encoding="utf-8"))
        elif args.command == "verify":
            manifest = read_json(args.job / "redaction-manifest.json")
            result = verify_output(Path(manifest["source"]["video"]), args.output / "sanitized.mp4")
            write_json(args.output / "verify.json", result)
            print(json.dumps(result, indent=2))
    except ToolError as exc:
        raise SystemExit(f"error: {exc}") from exc
    except KeyboardInterrupt:
        print("[priv-sanitizer] interrupted; stopping without publishing an incomplete output", file=os.sys.stderr)
        raise SystemExit(130)
