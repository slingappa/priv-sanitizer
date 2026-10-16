"""Optional transcript readers used only for hardening analysis."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


TIMESTAMP = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})(?:[,.](\d{1,3}))?\s*$")
SRT_RANGE = re.compile(r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})")
SPEAKER = re.compile(r"^([^:\n]{1,80}):\s*(.*)$")


def _seconds(value: str) -> float:
    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        h, m, s = parts
    else:
        h, m, s = "0", parts[0], parts[1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", ns):
        chunks = [node.text or "" for node in paragraph.findall(".//w:t", ns)]
        text = "".join(chunks).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        return _docx_text(path)
    return path.read_text(encoding="utf-8-sig", errors="replace")


def parse(path: Path) -> dict[str, Any]:
    text = read_text(path)
    lines = text.splitlines()
    cues: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        stripped = line.strip()
        match = TIMESTAMP.match(stripped)
        if match:
            if current:
                current["text"] = "\n".join(current.pop("lines")).strip()
                cues.append(current)
            current = {"start": _seconds(stripped), "lines": [], "timestamp": stripped}
            continue
        range_match = SRT_RANGE.search(stripped)
        if range_match:
            if current:
                current["text"] = "\n".join(current.pop("lines")).strip()
                cues.append(current)
            current = {
                "start": _seconds(range_match.group(1)),
                "end": _seconds(range_match.group(2)),
                "lines": [],
                "timestamp": stripped,
            }
            continue
        if current is not None:
            current["lines"].append(line)
    if current:
        current["text"] = "\n".join(current.pop("lines")).strip()
        cues.append(current)
    for index, cue in enumerate(cues):
        if "end" not in cue:
            cue["end"] = cues[index + 1]["start"] if index + 1 < len(cues) else cue["start"] + 30.0
        speakers: list[str] = []
        body_lines: list[str] = []
        for line in cue["text"].splitlines():
            speaker = SPEAKER.match(line.strip())
            if speaker:
                speakers.append(speaker.group(1).strip())
                body_lines.append(speaker.group(2).strip())
            else:
                body_lines.append(line.strip())
        cue["speakers"] = [
            s for s in speakers
            if re.fullmatch(r"[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,3}", s)
        ]
        cue["body_lines"] = [x for x in body_lines if x]
        cue["body"] = " ".join(x for x in body_lines if x).strip()
    names = sorted({speaker for cue in cues for speaker in cue.get("speakers", [])})
    return {"path": str(path.resolve()), "format": path.suffix.lower().lstrip("."), "cues": cues, "names": names}


def hardening_intervals(
    data: dict[str, Any], duration: float, *, time_offset: float = 0.0
) -> list[dict[str, Any]]:
    """Estimate short mute windows around spoken name matches.

    DOCX/Gemini cues are usually segment-level timestamps, not word-level
    timestamps.  We therefore use a token-position estimate and mark every
    interval for review.  A coarse cue must never turn into muting the whole
    cue or the whole speaker's audio.
    """
    names = data.get("names", [])
    tokens: list[str] = []
    for name in names:
        parts = [p for p in re.split(r"\s+", name) if len(p) >= 3]
        tokens.extend([name, *parts])
    unique = sorted(set(tokens), key=len, reverse=True)
    intervals: list[dict[str, Any]] = []
    for i, cue in enumerate(data.get("cues", [])):
        cue_start = float(cue["start"]) - time_offset
        cue_end = float(cue["end"]) - time_offset
        if cue_end <= 0 or cue_start >= duration:
            continue
        start_bound = max(0.0, cue_start)
        end_bound = min(duration, cue_end)
        body = cue.get("body", "")
        if not body or not unique:
            continue
        words = list(re.finditer(r"[A-Za-z][A-Za-z'’-]*", body))
        if not words:
            continue
        occupied: list[tuple[int, int]] = []
        for token in unique:
            for match in re.finditer(rf"\b{re.escape(token)}\b", body, re.IGNORECASE):
                if any(left <= match.start() < right or left < match.end() <= right for left, right in occupied):
                    continue
                occupied.append((match.start(), match.end()))
                before = len([word for word in words if word.start() < match.start()])
                token_count = max(1, len(re.findall(r"[A-Za-z][A-Za-z'’-]*", match.group(0))))
                cue_span = max(0.1, end_bound - start_bound)
                estimated_start = start_bound + cue_span * before / len(words)
                estimated_length = max(0.35, cue_span * token_count / len(words) + 0.18)
                start = max(start_bound, estimated_start - 0.12)
                end = min(end_bound, start + estimated_length)
                if end <= start:
                    continue
                intervals.append({
                    "id": f"audio-name-{i:04d}-{len(intervals):03d}",
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "kind": "spoken_participant_name",
                    "mode": "mute",
                    "matched_name": token,
                    "confidence": 0.55,
                    "timing_quality": "estimated-from-cue",
                    "sources": ["transcript_name_match", "cue_token_position"],
                    "review_required": True,
                })
    return intervals
