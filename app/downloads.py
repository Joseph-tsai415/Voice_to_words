"""Download queue for ASR models.

One model downloads at a time; the rest wait in line with a visible position.
The server is the single source of truth for download state - the browser only
renders what it polls, so a page reload or a list re-render never loses track of
an in-flight download, and clicking 下載 twice cannot start two jobs.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from . import hub
from .config import ASR_MODELS, is_downloaded, required_files

log = logging.getLogger("scribe.downloads")

# States a model can be in. "idle" is not stored - absence means idle.
QUEUED, RUNNING, DONE, ERROR, CANCELLED = "queued", "running", "done", "error", "cancelled"

STATE_LABELS = {
    QUEUED: "排隊中",
    RUNNING: "下載中",
    DONE: "完成",
    ERROR: "失敗",
    CANCELLED: "已取消",
}

# How long a finished/failed record stays visible before it is forgotten.
_KEEP_FINISHED_SEC = 25.0


class DownloadManager:
    def __init__(self, max_concurrent: int = 1) -> None:
        self.max_concurrent = max_concurrent
        self._records: dict[str, dict[str, Any]] = {}
        self._queue: deque[str] = deque()
        self._running: set[str] = set()
        self._cancel: set[str] = set()
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ API
    def enqueue(self, key: str) -> dict[str, Any]:
        """Queue a model. Idempotent: returns the existing record if already
        queued or running."""
        if key not in ASR_MODELS:
            raise KeyError(f"未知的模型：{key}")
        if ASR_MODELS[key]["repo"] is None:
            raise ValueError("內建模型不需要下載。")

        with self._guard:
            record = self._records.get(key)
            if record and record["state"] in (QUEUED, RUNNING):
                log.info("模型 %s 已在佇列中（%s），不重複加入", key, record["state"])
                return dict(record)

            if is_downloaded(key):
                return {"key": key, "state": DONE, "progress": 1.0,
                        "detail": "已完整", "label": ASR_MODELS[key]["label"]}

            self._cancel.discard(key)
            self._records[key] = {
                "key": key,
                "label": ASR_MODELS[key]["label"],
                "state": QUEUED,
                "progress": 0.0,
                "detail": "等待中",
                "error": None,
                "bytes_done": 0,
                "bytes_total": 0,
                "speed": 0.0,
                "eta": None,
                "file_index": 0,
                "file_count": len(required_files(key)),
                "queued_at": time.time(),
                "finished_at": None,
            }
            self._queue.append(key)
            log.info("模型 %s 加入下載佇列（前面還有 %d 個）", key, len(self._queue) - 1)
            self._pump_locked()
            return dict(self._records[key])

    def cancel(self, key: str) -> bool:
        """Cancel a queued or running download."""
        with self._guard:
            record = self._records.get(key)
            if not record or record["state"] not in (QUEUED, RUNNING):
                return False
            self._cancel.add(key)
            if record["state"] == QUEUED:
                try:
                    self._queue.remove(key)
                except ValueError:
                    pass
                self._finish_locked(key, CANCELLED, "已取消")
                self._pump_locked()
            else:
                record["detail"] = "取消中…"
            log.info("模型 %s 取消下載", key)
            return True

    def status(self, key: str) -> dict[str, Any] | None:
        with self._guard:
            self._expire_locked()
            record = self._records.get(key)
            if not record:
                return None
            out = dict(record)
            out["position"] = self._position_locked(key)
            out["state_label"] = STATE_LABELS.get(out["state"], out["state"])
            return out

    def all_status(self) -> dict[str, dict[str, Any]]:
        with self._guard:
            self._expire_locked()
            out = {}
            for key, record in self._records.items():
                item = dict(record)
                item["position"] = self._position_locked(key)
                item["state_label"] = STATE_LABELS.get(item["state"], item["state"])
                out[key] = item
            return out

    def is_active(self, key: str) -> bool:
        with self._guard:
            record = self._records.get(key)
            return bool(record and record["state"] in (QUEUED, RUNNING))

    def active_count(self) -> int:
        with self._guard:
            return sum(1 for r in self._records.values() if r["state"] in (QUEUED, RUNNING))

    # -------------------------------------------------------------- internals
    def _position_locked(self, key: str) -> int:
        """0 = downloading now, 1+ = places back in the queue."""
        record = self._records.get(key)
        if not record:
            return -1
        if record["state"] == RUNNING:
            return 0
        if record["state"] == QUEUED:
            try:
                return self._queue.index(key) + 1
            except ValueError:
                return -1
        return -1

    def _expire_locked(self) -> None:
        now = time.time()
        for key in [k for k, r in self._records.items()
                    if r.get("finished_at") and now - r["finished_at"] > _KEEP_FINISHED_SEC]:
            self._records.pop(key, None)

    def _finish_locked(self, key: str, state: str, detail: str, error: str | None = None) -> None:
        # Free the slot first - see the note in transcribe_queue._finish_locked.
        self._running.discard(key)
        self._cancel.discard(key)
        record = self._records.get(key)
        if not record:
            return
        record.update(state=state, detail=detail, error=error,
                      finished_at=time.time(), speed=0.0, eta=None)
        if state == DONE:
            record["progress"] = 1.0

    def _pump_locked(self) -> None:
        """Start as many queued downloads as the concurrency cap allows."""
        while self._queue and len(self._running) < self.max_concurrent:
            key = self._queue.popleft()
            record = self._records.get(key)
            if not record or record["state"] != QUEUED:
                continue
            record.update(state=RUNNING, detail="連線中…", started_at=time.time())
            self._running.add(key)
            threading.Thread(target=self._worker, args=(key,),
                             name=f"download-{key}", daemon=True).start()

    def _worker(self, key: str) -> None:
        start = time.time()

        def progress(_stage: str, frac: float, detail: str) -> None:
            with self._guard:
                record = self._records.get(key)
                if not record:
                    return
                elapsed = max(1e-6, time.time() - start)
                record["progress"] = max(0.0, min(1.0, frac))
                record["detail"] = detail
                if frac > 0.01:
                    record["eta"] = round(elapsed * (1 - frac) / frac)

        def cancelled() -> bool:
            with self._guard:
                return key in self._cancel

        try:
            hub.download_model(key, progress=progress, cancelled=cancelled)
        except hub.DownloadCancelled:
            with self._guard:
                self._finish_locked(key, CANCELLED, "已取消")
                self._pump_locked()
            log.info("模型 %s 下載已取消", key)
            return
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.exception("模型 %s 下載失敗", key)
            with self._guard:
                self._finish_locked(key, ERROR, "下載失敗", message)
                self._pump_locked()
            return

        with self._guard:
            self._finish_locked(key, DONE, "下載完成")
            self._pump_locked()
        log.info("模型 %s 下載完成（耗時 %.0f 秒）", key, time.time() - start)


manager = DownloadManager(max_concurrent=1)
