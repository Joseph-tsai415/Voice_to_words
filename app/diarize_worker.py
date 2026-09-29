"""Speaker diarization in a separate process.

sherpa-onnx's OfflineSpeakerDiarization.process() holds the GIL for its whole
run - measured 100% blocked, an 18.9-second stall on a 3-minute recording. A
progress callback drops that to ~30%, but 4-second freezes still made the web UI
unusable: opening a dialog or switching projects would hang.

ASR does not have this problem (decode_streams releases the GIL cleanly), so only
diarization is exiled. Its two models total ~46 MB, which makes a child process
cheap - unlike the ~1 GB recogniser, which stays shared in the parent.

Run directly for a self-test:
    python -m app.diarize_worker <wav> [num_speakers]
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("scribe.diarize")

ProgressFn = Callable[[str, float, str], None]

# Warn (do not fail) if the child goes quiet for this long.
STALL_WARN_SEC = 120.0
# A 25-minute recording produces ~2000 chunks. Reporting each one is thousands
# of IPC writes for progress nobody can read that fast.
PROGRESS_STEP = 0.004


# ---------------------------------------------------------------------------
# Child side
# ---------------------------------------------------------------------------
def _run_child(request: dict) -> dict:
    import numpy as np
    import soundfile as sf

    from .asr import diarize

    samples, _ = sf.read(request["wav"], dtype="float32", always_2d=False)
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim > 1:
        samples = samples.mean(axis=1, dtype=np.float32)

    start = request.get("start")
    end = request.get("end")
    if start is not None and end is not None:
        rate = request.get("sample_rate", 16000)
        samples = samples[int(start * rate):int(end * rate)]

    last = [-1.0]

    def progress(_stage: str, frac: float, detail: str) -> None:
        # stderr carries progress; stdout is reserved for the result.
        # Throttled: a 25-minute recording is ~2000 chunks, and one IPC write
        # per chunk is thousands of writes nobody can read that fast.
        if frac - last[0] < PROGRESS_STEP and frac < 1.0:
            return
        last[0] = frac
        sys.stderr.write(f"@P {frac:.6f} {detail}\n")
        sys.stderr.flush()

    turns = diarize(
        samples,
        num_speakers=request.get("num_speakers", -1),
        threshold=request.get("threshold", 0.5),
        num_threads=request.get("num_threads", 2),
        progress=progress,
    )
    return {"turns": [[s, e, spk] for s, e, spk in turns]}


def _main() -> int:
    logging.basicConfig(level=logging.WARNING)
    if len(sys.argv) > 1:                      # manual self-test
        request = {"wav": sys.argv[1],
                   "num_speakers": int(sys.argv[2]) if len(sys.argv) > 2 else -1}
    else:
        request = json.loads(sys.stdin.read())
    try:
        result = _run_child(request)
    except Exception as exc:                   # reported to the parent as data
        result = {"error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Parent side
# ---------------------------------------------------------------------------
class DiarizationFailed(RuntimeError):
    pass


def diarize_in_process(wav: str | Path, *, num_speakers: int = -1,
                       threshold: float = 0.5, num_threads: int = 2,
                       start: float | None = None, end: float | None = None,
                       progress: ProgressFn | None = None,
                       cancelled: Callable[[], bool] | None = None,
                       timeout: float = 3600) -> list[tuple[float, float, int]]:
    """Diarize `wav` in a child process, keeping this process responsive."""
    request = {
        "wav": str(Path(wav).resolve()),
        "num_speakers": num_speakers,
        "threshold": threshold,
        "num_threads": num_threads,
        "start": start,
        "end": end,
    }

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.diarize_worker"],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    # The pipes are drained by dedicated threads. communicate() must NOT be used
    # alongside them: it starts its own readers for the same pipes, and two
    # readers on one pipe lose data and hang - progress froze on its first line
    # and the call never returned.
    noise: list[str] = []
    collected: list[str] = []
    last_progress = [time.monotonic()]

    def pump_stdout() -> None:
        try:
            collected.append(proc.stdout.read())       # type: ignore[union-attr]
        except Exception:
            pass

    def pump_stderr() -> None:
        try:
            for line in proc.stderr:                   # type: ignore[union-attr]
                line = line.rstrip("\r\n")
                if line.startswith("@P "):
                    last_progress[0] = time.monotonic()
                    if progress:
                        try:
                            _, frac, *rest = line.split(" ", 2)
                            progress("diarize", float(frac), rest[0] if rest else "")
                        except (ValueError, IndexError):
                            pass
                elif line.strip():
                    noise.append(line)
        except Exception:
            pass

    def feed_stdin() -> None:
        try:
            proc.stdin.write(json.dumps(request))      # type: ignore[union-attr]
            proc.stdin.close()                         # type: ignore[union-attr]
        except Exception:
            pass

    threads = [threading.Thread(target=fn, daemon=True)
               for fn in (feed_stdin, pump_stdout, pump_stderr)]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancelled and cancelled():
            log.info("取消講者分離，結束子行程")
            proc.kill()
            break
        if time.monotonic() > deadline:
            proc.kill()
            raise DiarizationFailed(f"講者分離超過 {timeout:.0f} 秒仍未完成。")
        if time.monotonic() - last_progress[0] > STALL_WARN_SEC:
            log.warning("講者分離已 %.0f 秒沒有回報進度", STALL_WARN_SEC)
            last_progress[0] = time.monotonic()
        time.sleep(0.2)

    for thread in threads:
        thread.join(timeout=5)
    out = "".join(collected)

    if cancelled and cancelled():
        return []
    if proc.returncode != 0 and not out.strip():
        detail = " / ".join(noise[-3:]) or f"結束碼 {proc.returncode}"
        raise DiarizationFailed(f"講者分離子行程失敗：{detail}")

    try:
        result = json.loads(out)
    except json.JSONDecodeError as exc:
        raise DiarizationFailed(
            f"講者分離子行程沒有回傳有效結果：{out[:200]}") from exc
    if "error" in result:
        raise DiarizationFailed(result["error"])

    return [(float(a), float(b), int(c)) for a, b, c in result["turns"]]


if __name__ == "__main__":
    raise SystemExit(_main())
