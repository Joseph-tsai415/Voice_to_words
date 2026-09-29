"""Render a finished project as a transcript in various formats."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from .config import BUBBLE_GAP_SEC
from .project import bubbles, speaker_map

FORMATS = {
    "txt": ("純文字逐字稿", "text/plain; charset=utf-8", ".txt"),
    "md": ("Markdown", "text/markdown; charset=utf-8", ".md"),
    "srt": ("字幕檔 SRT", "application/x-subrip; charset=utf-8", ".srt"),
    "vtt": ("字幕檔 WebVTT", "text/vtt; charset=utf-8", ".vtt"),
    "csv": ("試算表 CSV", "text/csv; charset=utf-8", ".csv"),
    "json": ("原始資料 JSON", "application/json; charset=utf-8", ".json"),
}


def _clock(seconds: float, sep: str = ",") -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _short_clock(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def as_txt(project: dict[str, Any], timestamps: bool = True, merge: bool = True) -> str:
    names = speaker_map(project)
    lines = [project.get("name", "逐字稿"), "=" * 40, ""]

    if merge:
        for group in bubbles(project, BUBBLE_GAP_SEC):
            who = names.get(group["speaker"], {}).get("name", group["speaker"])
            stamp = f"[{_short_clock(group['start'])}] " if timestamps else ""
            body = "".join(s["text"] for s in group["segments"])
            lines.append(f"{stamp}{who}：{body}")
            lines.append("")
    else:
        for seg in project["segments"]:
            who = names.get(seg["speaker"], {}).get("name", seg["speaker"])
            stamp = f"[{_short_clock(seg['start'])}] " if timestamps else ""
            lines.append(f"{stamp}{who}：{seg['text']}")
    return "\n".join(lines).rstrip() + "\n"


def as_markdown(project: dict[str, Any], timestamps: bool = True) -> str:
    names = speaker_map(project)
    out = [f"# {project.get('name', '逐字稿')}", ""]
    out.append(f"- 長度：{_short_clock(project.get('duration', 0))}")
    out.append(f"- 句數：{len(project.get('segments', []))}")
    out.append("- 講者：" + "、".join(s["name"] for s in project.get("speakers", [])))
    out.append("")
    out.append("---")
    out.append("")
    for group in bubbles(project, BUBBLE_GAP_SEC):
        who = names.get(group["speaker"], {}).get("name", group["speaker"])
        stamp = f" `{_short_clock(group['start'])}`" if timestamps else ""
        out.append(f"**{who}**{stamp}")
        out.append("")
        out.append("".join(s["text"] for s in group["segments"]))
        out.append("")
    return "\n".join(out)


def as_srt(project: dict[str, Any]) -> str:
    names = speaker_map(project)
    out = []
    for i, seg in enumerate(project["segments"], start=1):
        who = names.get(seg["speaker"], {}).get("name", seg["speaker"])
        out.append(str(i))
        out.append(f"{_clock(seg['start'])} --> {_clock(seg['end'])}")
        out.append(f"{who}：{seg['text']}")
        out.append("")
    return "\n".join(out)


def as_vtt(project: dict[str, Any]) -> str:
    names = speaker_map(project)
    out = ["WEBVTT", ""]
    for seg in project["segments"]:
        who = names.get(seg["speaker"], {}).get("name", seg["speaker"])
        out.append(f"{_clock(seg['start'], '.')} --> {_clock(seg['end'], '.')}")
        out.append(f"<v {who}>{seg['text']}")
        out.append("")
    return "\n".join(out)


def as_csv(project: dict[str, Any]) -> str:
    names = speaker_map(project)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["序號", "開始", "結束", "講者", "內容", "已修改文字", "已改講者"])
    for i, seg in enumerate(project["segments"], start=1):
        writer.writerow([
            i,
            _clock(seg["start"], "."),
            _clock(seg["end"], "."),
            names.get(seg["speaker"], {}).get("name", seg["speaker"]),
            seg["text"],
            "是" if seg.get("edited") else "",
            "是" if seg.get("speaker_edited") else "",
        ])
    return buf.getvalue()


def as_json(project: dict[str, Any]) -> str:
    return json.dumps(project, ensure_ascii=False, indent=2)


def render(project: dict[str, Any], fmt: str, timestamps: bool = True, merge: bool = True) -> str:
    if fmt == "txt":
        return as_txt(project, timestamps, merge)
    if fmt == "md":
        return as_markdown(project, timestamps)
    if fmt == "srt":
        return as_srt(project)
    if fmt == "vtt":
        return as_vtt(project)
    if fmt == "csv":
        return as_csv(project)
    if fmt == "json":
        return as_json(project)
    raise ValueError(f"不支援的格式：{fmt}")
