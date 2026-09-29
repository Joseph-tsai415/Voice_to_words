"""Re-recognition checks: a whole project, and one sentence on its own.

The interesting case is a sentence that actually holds several speakers. The
test builds one deliberately - three voices glued together with no gaps, fed in
as a single segment - and asserts the re-run splits it and puts the pieces back
under the speakers the project already had.

Run:  .venv/Scripts/python.exe tests/test_rework.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np                                          # noqa: E402
import soundfile as sf                                      # noqa: E402

from app import project as store, rework, voiceprint        # noqa: E402
from app.audio import load_16k_mono                         # noqa: E402
from app.config import SAMPLE_RATE                          # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_e2e import installed_voices, _ps                  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


LINES = [
    "各位早安，我們今天討論第三季的預算分配，請大家先看手上的資料。",
    "好的，行銷部門的數字總共是三百二十萬，比上一季增加了不少。",
    "我補充一下，研發那邊的支出也要一起算進來才準確。",
]


def build_multi_voice(dest: Path) -> tuple[Path, int]:
    """One continuous clip of N different voices with no silence between them.

    Glued tight on purpose: this is the recording that a single pass tends to
    return as one long sentence.
    """
    voices = installed_voices()
    if len(voices) < 2:
        raise RuntimeError("需要至少兩個 SAPI 語音才能測試")
    chosen = (voices * 3)[:3]

    dest.parent.mkdir(parents=True, exist_ok=True)
    for stale in dest.parent.glob("mv*.wav"):
        stale.unlink()

    script = ["Add-Type -AssemblyName System.Speech"]
    for i, text in enumerate(LINES):
        script += [
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
            f'$s.SelectVoice("{chosen[i % len(chosen)]}")',
            f"$s.Rate = {[-2, 1, 3][i % 3]}",
            f'$s.SetOutputToWaveFile("{dest.parent / f"mv{i}.wav"}")',
            f'$s.Speak("{text}")',
            "$s.Dispose()",
        ]
    proc = _ps("\n".join(script))
    if proc.returncode != 0:
        raise RuntimeError(f"SAPI 失敗：{proc.stderr[:200]}")

    import soxr
    parts = []
    for wav in sorted(dest.parent.glob("mv*.wav")):
        x, sr = sf.read(str(wav), dtype="float32", always_2d=False)
        if x.ndim > 1:
            x = x.mean(axis=1, dtype="float32")
        if sr != SAMPLE_RATE:
            x = soxr.resample(x, sr, SAMPLE_RATE).astype("float32")
        parts.append(x)
        parts.append(np.zeros(int(0.12 * SAMPLE_RATE), dtype="float32"))   # tight
    sf.write(str(dest), np.concatenate(parts), SAMPLE_RATE, subtype="PCM_16")
    distinct = len(set(chosen[:len(LINES)]))
    return dest, distinct


def make_project(wav: Path, whole_as_one: bool) -> str:
    """A project whose transcript is deliberately wrong: everything in one
    segment attributed to a single speaker."""
    proj = store.create("重新辨識測試", 0.0, "sense-voice", "s2twp",
                        {"model": "sense-voice", "zh_mode": "s2twp",
                         "num_speakers": -1, "threads": 4, "language": ""})
    pid = proj["id"]
    shutil.copy(wav, store.project_dir(pid) / "audio.wav")

    samples = load_16k_mono(wav)
    total = len(samples) / SAMPLE_RATE
    store.set_duration(pid, total)

    project = store.load(pid)
    project["speakers"] = [
        {"id": "S1", "name": "Joseph", "color": "#2563eb", "cluster": 0},
        {"id": "S2", "name": "Rebecca", "color": "#db2777", "cluster": 1},
    ]
    if whole_as_one:
        project["segments"] = [{
            "id": "seg-0000", "start": 0.0, "end": round(total, 3),
            "speaker": "S1", "text": "（整段被當成一句）",
            "original_text": "（整段被當成一句）", "original_speaker": "S1",
            "edited": False, "speaker_edited": False,
        }]
    project["status"] = "ready"
    store.save(project)
    return pid


def test_segment_rerun() -> None:
    section("重新辨識單一句子（一句裡有多人說話）")
    scratch = Path(__import__("os").environ.get("TEMP", ".")) / "scribe-rework"
    wav, distinct = build_multi_voice(scratch / "multi.wav")
    print(f"  測試音檔：{len(LINES)} 段連續發言、{distinct} 個不同聲音")

    pid = make_project(wav, whole_as_one=True)
    try:
        before = store.load(pid)
        check("重辨識前只有 1 句", len(before["segments"]) == 1)

        result = rework.rerun_segment(pid, "seg-0000", num_speakers=distinct)
        project, summary = result["project"], result["summary"]

        check("拆成多句", len(project["segments"]) > 1,
              f"{len(project['segments'])} 句")
        check("每句都有文字",
              all(s["text"].strip() for s in project["segments"]),
              " | ".join(s["text"][:14] for s in project["segments"][:3]))
        check("時間落在原本的範圍內",
              all(0 <= s["start"] < s["end"] <= before["segments"][0]["end"] + 0.05
                  for s in project["segments"]),
              f"{project['segments'][0]['start']:.2f}–{project['segments'][-1]['end']:.2f}")
        check("句子按時間排序",
              all(a["end"] <= b["start"] + 0.05 for a, b in
                  zip(project["segments"], project["segments"][1:])))
        check("認出多位講者", len(summary["speakers"]) > 1,
              f"{summary['speakers']}")
        check("原講者保留最大的一段（沒有聲紋可比對時）",
              any(s["speaker"] == "S1" for s in project["segments"]),
              f"用到 {sorted({s['speaker'] for s in project['segments']})}")
        check("每句都有比對分數",
              all(s.get("match_score") is not None for s in project["segments"]),
              str(summary["scores"][:3]))

        ids = [s["id"] for s in project["segments"]]
        check("句子 id 沒有重複", len(ids) == len(set(ids)), str(ids[:4]))

        # 太短的句子要擋下來，而不是硬跑
        tiny = dict(project["segments"][0])
        tiny.update(id="seg-tiny", start=1.0, end=1.2)
        p = store.load(pid)
        p["segments"].append(tiny)
        store.save(p)
        try:
            rework.rerun_segment(pid, "seg-tiny", num_speakers=2)
            check("太短的句子會被擋下", False, "沒有拋出例外")
        except ValueError as exc:
            check("太短的句子會被擋下", "太短" in str(exc), str(exc)[:40])
    finally:
        store.delete(pid)


def test_full_rerun_clears() -> None:
    section("整個專案重新辨識")
    scratch = Path(__import__("os").environ.get("TEMP", ".")) / "scribe-rework"
    wav = scratch / "multi.wav"
    pid = make_project(wav, whole_as_one=True)
    try:
        options = rework.prepare_full_rerun(pid, {"model": "sense-voice",
                                                  "num_speakers": 3,
                                                  "zh_mode": "s2tw"})
        project = store.load(pid)
        check("逐字稿已清空", project["segments"] == [])
        check("講者已清空", project["speakers"] == [])
        check("狀態回到排隊中", project["status"] == "queued", project["status"])
        check("採用新的設定", options["num_speakers"] == 3 and options["zh_mode"] == "s2tw",
              str({k: options[k] for k in ("num_speakers", "zh_mode")}))
        check("記錄重跑次數", project.get("reruns") == 1, str(project.get("reruns")))
        check("音檔仍在", (store.project_dir(pid) / "audio.wav").exists())
    finally:
        store.delete(pid)


def test_voiceprint_separation() -> None:
    section("聲紋分辨能力")
    scratch = Path(__import__("os").environ.get("TEMP", ".")) / "scribe-rework"
    samples = load_16k_mono(scratch / "multi.wav")
    from app.asr import diarize

    turns = diarize(samples, num_speakers=-1, num_threads=4)
    vecs = []
    for start, end, spk in turns:
        vec = voiceprint.embed(samples[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)])
        if vec is not None:
            vecs.append((spk, vec))

    same = [float(a @ b) for i, (ka, a) in enumerate(vecs)
            for kb, b in vecs[i + 1:] if ka == kb]
    cross = [float(a @ b) for i, (ka, a) in enumerate(vecs)
             for kb, b in vecs[i + 1:] if ka != kb]
    if same and cross:
        check("同一人比不同人更相似",
              min(same) > max(cross),
              f"同 {min(same):.3f}↓ vs 異 {max(cross):.3f}↑")
        check("門檻落在兩者之間",
              max(cross) < voiceprint.MATCH_THRESHOLD < min(same),
              f"{max(cross):.3f} < {voiceprint.MATCH_THRESHOLD} < {min(same):.3f}")
    else:
        check("聲紋分辨能力", True, "（只有一位講者，略過）")


def main() -> int:
    test_segment_rerun()
    test_full_rerun_clears()
    test_voiceprint_separation()

    print()
    if failures:
        print(f"  {len(failures)} 項失敗：")
        for f in failures:
            print("    - " + f)
        return 1
    print("  全部通過")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
