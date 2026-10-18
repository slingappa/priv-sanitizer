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
        help="emit FFmpeg commands and periodic heartbeats for long-running stages",
    )


def _add_quiet_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
        help="suppress status/progress diagnostics and verbose.log for this run",
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
"""
    parser = argparse.ArgumentParser(
        prog="priv-sanitizer",
        description="Sanitize meeting video locally",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="emit FFmpeg commands and periodic heartbeats for long-running stages",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="suppress status/progress diagnostics and verbose.log for this run",
    )
    sub = parser.add_subparsers(dest="command", required=False)
    parser.set_defaults(
        command="batch",
        input=Path.cwd(),
        output=Path.cwd() / "outputs_sanitized",
        artifacts=None,
        experimental_audio=False,
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
    inspect.add_argument("video", type=_path)
    inspect.add_argument("--job", type=Path, required=True)

    analyze = sub.add_parser("analyze", help="run baseline video-only analysis")
    _add_verbose_option(analyze)
    _add_quiet_option(analyze)
    analyze.add_argument("job", type=Path)
    analyze.add_argument("--pass", dest="analysis_pass", choices=["baseline"], default="baseline")
    analyze.add_argument("--audio-mode", choices=["preserve", "mute-names"], default="preserve")
    analyze.add_argument("--every", type=float, default=4.0)

    sanitize = sub.add_parser("sanitize", help="run baseline analysis and render")
    _add_verbose_option(sanitize)
    _add_quiet_option(sanitize)
    sanitize.add_argument("--video", type=_path, required=True)
    sanitize.add_argument("--output", type=Path, required=True)
    sanitize.add_argument("--audio-mode", choices=["preserve", "mute-names"], default="preserve")
    sanitize.add_argument("--every", type=float, default=4.0)

    harden_cmd = sub.add_parser("harden", help="apply optional transcript-assisted hardening")
    _add_verbose_option(harden_cmd)
    _add_quiet_option(harden_cmd)
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
    render.add_argument("job", type=Path)
    render.add_argument("--output", type=Path, required=True)

    report = sub.add_parser("report", help="print a job report")
    _add_verbose_option(report)
    _add_quiet_option(report)
    report.add_argument("job", type=Path)

    verify = sub.add_parser("verify", help="verify a rendered output")
    _add_verbose_option(verify)
    _add_quiet_option(verify)
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
    try:
        if args.command == "batch":
            print(json.dumps(
                batch(
                    args.input,
                    args.output,
                    artifacts_dir=args.artifacts,
                    experimental_audio=args.experimental_audio,
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
