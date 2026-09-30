"""Transcription-queue checks.

The slot-leak regression here is the one that matters: deleting a project while
it was still running used to strand its concurrency slot, and after a couple of
those the queue silently stopped starting anything at all.

Run:  .venv/Scripts/python.exe tests/test_queue.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import transcribe_queue as tqm          # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


def wait_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return False


class FakeWork:
    """Replaces the worker body: sleeps, honours cancel, counts live slots."""

    def __init__(self, queue: tqm.TranscriptionQueue, seconds: float = 1.5) -> None:
        self.q = queue
        self.seconds = seconds
        self.started: list[str] = []
        self.live = 0
        self.max_live = 0
        self.guard = threading.Lock()
        self._install()

    def _install(self) -> None:
        outer = self

        def worker(project_id: str) -> None:
            with outer.guard:
                outer.started.append(project_id)
                outer.live += 1
                outer.max_live = max(outer.max_live, outer.live)
            try:
                steps = 30
                for _ in range(steps):
                    if outer.q._cancelled(project_id):
                        break
                    time.sleep(outer.seconds / steps)
            finally:
                with outer.guard:
                    outer.live -= 1
                with outer.q._guard:
                    outer.q._finish_locked(project_id, tqm.DONE, "done", "完成")
                    outer.q._pump_locked()

        self.q._worker = worker      # type: ignore[method-assign]


def fresh(max_concurrent: int = 2, seconds: float = 1.5):
    q = tqm.TranscriptionQueue(max_concurrent=max_concurrent)
    return q, FakeWork(q, seconds)


def submit(q, name: str):
    return q.submit(name, f"/tmp/{name}.wav", {"model": "sense-voice"})


def state_of(q, name: str) -> str | None:
    return (q.status(name) or {}).get("state")


def test_concurrency() -> None:
    section("並行與排隊")
    q, fake = fresh(max_concurrent=2, seconds=1.0)
    names = [f"p{i}" for i in range(5)]
    for n in names:
        submit(q, n)

    check("同時只跑 2 個",
          wait_until(lambda: len(fake.started) == 2) and fake.max_live == 2,
          f"max_live={fake.max_live}")
    running = [n for n in names if state_of(q, n) == "running"]
    queued = [n for n in names if state_of(q, n) == "queued"]
    check("其餘進入排隊", len(running) == 2 and len(queued) == 3,
          f"running={running} queued={queued}")
    check("排隊位置連續 1,2,3",
          sorted((q.status(n) or {}).get("position") for n in queued) == [1, 2, 3],
          str([(n, (q.status(n) or {}).get("position")) for n in queued]))

    check("全部完成", wait_until(lambda: q.active_count() == 0, timeout=30))
    check("5 個都跑過", sorted(fake.started) == names, str(fake.started))
    check("全程沒有超額並行", fake.max_live == 2, f"max={fake.max_live}")


def test_slot_not_leaked_on_delete() -> None:
    section("刪除執行中的專案不會卡住佇列（回歸測試）")
    q, fake = fresh(max_concurrent=2, seconds=1.2)
    for i in range(4):
        submit(q, f"d{i}")
    wait_until(lambda: len(fake.started) == 2)

    # 在兩個跑中的專案還沒結束時就刪掉它們
    for name in list(fake.started):
        q.cancel(name)
        q.forget(name)

    check("被刪除後佇列繼續推進",
          wait_until(lambda: len(fake.started) == 4, timeout=25),
          f"實際啟動了 {len(fake.started)} 個")
    check("佇列最後清空", wait_until(lambda: q.active_count() == 0, timeout=25))
    check("沒有洩漏名額", len(q._running) == 0, f"_running={q._running}")

    submit(q, "after")
    check("刪除之後新工作仍能立即開始",
          wait_until(lambda: state_of(q, "after") == "running", timeout=10),
          str(state_of(q, "after")))
    wait_until(lambda: q.active_count() == 0, timeout=20)


def test_forget_only() -> None:
    section("只 forget 不 cancel 也要放回名額")
    q, fake = fresh(max_concurrent=1, seconds=1.0)
    submit(q, "x0")
    submit(q, "x1")
    wait_until(lambda: len(fake.started) == 1)
    q.forget("x0")                 # 直接刪掉，沒有先 cancel
    check("下一個仍會開始", wait_until(lambda: len(fake.started) == 2, timeout=20),
          str(fake.started))
    wait_until(lambda: q.active_count() == 0, timeout=20)
    check("名額已歸還", len(q._running) == 0, f"_running={q._running}")


def test_idempotent_submit() -> None:
    section("重複送出同一個專案")
    q, fake = fresh(max_concurrent=1, seconds=1.0)
    submit(q, "one")
    wait_until(lambda: len(fake.started) == 1)
    submit(q, "one")
    submit(q, "one")
    check("重複 submit 不會跑第二次", len(fake.started) == 1, str(fake.started))
    wait_until(lambda: q.active_count() == 0, timeout=20)


def test_cancel_queued() -> None:
    section("取消排隊中的專案")
    q, fake = fresh(max_concurrent=1, seconds=1.2)
    submit(q, "a")
    submit(q, "b")
    wait_until(lambda: len(fake.started) == 1)
    check("取消排隊中的", q.cancel("b"))
    check("被取消的不會開跑",
          wait_until(lambda: q.active_count() == 0, timeout=20) and "b" not in fake.started,
          str(fake.started))
    check("狀態記為已取消", state_of(q, "b") == "cancelled", str(state_of(q, "b")))


def test_progress_never_goes_backwards() -> None:
    """The bar must not collapse on the last step.

    STAGE_WEIGHTS covers decode/load/diarize/vad/asr, and _overall() returns
    0.0 for anything it does not know. "save" is reported *after* all of those
    have finished, so it was being reported as 0% - the bar went from full to
    empty at the exact moment the user is watching hardest.
    """
    section("進度不會倒退")
    stages = ['decode', 'load', 'diarize', 'vad', 'asr', 'save']
    values = [tqm._overall(s, 1.0) for s in stages]
    for name, value in zip(stages, values):
        check(f"{name} 有合理的進度", value > 0, f"{value:.2f}")
    check("整段流程單調遞增",
          all(values[i] <= values[i + 1] for i in range(len(values) - 1)),
          str([round(v, 2) for v in values]))
    check("最後一步是 100%", values[-1] == 1.0, f"{values[-1]:.2f}")
    # Waiting for a model download really has done nothing yet.
    check("等模型下載時是 0%", tqm._overall('wait_model', 1.0) == 0.0)

    check("權重加起來剛好是 1",
          abs(sum(tqm.STAGE_WEIGHTS.values()) - 1.0) < 1e-9,
          str(round(sum(tqm.STAGE_WEIGHTS.values()), 4)))

    # Measured end to end on a real 25-minute recording: diarize 183s, vad
    # 16s, asr 83s. The weights had diarize at 0.28 and asr at 0.60 - exactly
    # transposed - so the bar crawled through the longest stage and then
    # jumped. On a GPU the gap is wider still, because only asr gets faster.
    w = tqm.STAGE_WEIGHTS
    check("分辨講者的權重比辨識文字大（實測它才是大頭）",
          w['diarize'] > w['asr'], f"diarize={w['diarize']} asr={w['asr']}")
    check("分辨講者權重接近實測的 0.65",
          abs(w['diarize'] - 0.60) < 0.08, str(w['diarize']))
    check("辨識文字權重接近實測的 0.29",
          abs(w['asr'] - 0.28) < 0.08, str(w['asr']))


def main() -> int:
    test_progress_never_goes_backwards()
    test_concurrency()
    test_slot_not_leaked_on_delete()
    test_forget_only()
    test_idempotent_submit()
    test_cancel_queued()

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
