"""Transcription queue - many meetings in flight, each with its own progress.

Uploading creates a project immediately and hands it to this queue. Nothing
blocks the browser: the project appears in the list right away and carries its
own live status, so you can queue a dozen recordings and watch them work through.

A few run at once (`max_concurrent`); the rest wait with a visible queue
position. Recognizers are cached and shared, so two jobs on the same model do
not load it twice.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from . import asr, downloads, project as store
from .audio import AudioError, duration_of, load_16k_mono
from .config import ASR_MODELS, MAX_CONCURRENT_JOBS, is_downloaded

log = logging.getLogger("scribe.transcribe")

QUEUED, RUNNING, DONE, ERROR, CANCELLED = "queued", "running", "done", "error", "cancelled"

STAGE_LABELS = {
    "queued": "排隊中",
    "wait_model": "等待模型下載",
    "decode": "讀取音檔",
    "load": "載入模型",
    "diarize": "分辨講者",
    "vad": "切分語句",
    "asr": "語音辨識",
    "save": "儲存",
    "done": "完成",
    "error": "失敗",
    "cancelled": "已取消",
}

# Share of wall-clock each stage takes, for a single overall percentage.
# Measured end to end on a real 25-minute recording: diarize 183s, vad 16s,
# asr 83s - so 65% / 6% / 29% of the proportional work. These were previously
# 0.28 / 0.05 / 0.60, i.e. diarize and asr transposed, which made the bar
# crawl through the longest stage and then jump near the end.
#
# That is the CPU case, and it is the conservative one: on a GPU only asr gets
# faster (14x measured), so diarize's share goes up, not down.
STAGE_WEIGHTS = {"decode": 0.04, "load": 0.03, "diarize": 0.60, "vad": 0.05, "asr": 0.28}
_ORDER = ["decode", "load", "diarize", "vad", "asr"]

# Stages whose cost scales with the length of the recording. Only these are used
# to project a finish time; decode and model-load are roughly fixed overheads.
_PROPORTIONAL = frozenset({"diarize", "vad", "asr"})

_FIXED_WEIGHT = STAGE_WEIGHTS["decode"] + STAGE_WEIGHTS["load"]


# Stages that sit outside the weighted pipeline because they are not part of
# the proportional work. "save" runs after every weighted stage has finished,
# so it is 100% - returning the 0.0 that an unknown stage gets made the bar
# collapse from full to empty on the very last step.
_TERMINAL = frozenset({"save", "done"})


def disposable_upload(source: Path, wav: Path) -> bool:
    """Whether `source` is the throwaway upload rather than the recording.

    The worker deletes its source file once a run succeeds, because
    `source.<ext>` is redundant as soon as `audio.wav` has been written. But
    `_rerun_source()` deliberately feeds a re-run **audio.wav itself** - the
    upload is long gone by then - so source and the project's only copy of
    the recording were the same file, and every successful 重新辨識 deleted
    it. Seen twice on a live install: a project left with a transcript and no
    audio, and so no way to ever run it again.

    Only a file actually named `source...` is disposable, and never the wav,
    compared case-insensitively because Windows paths are.
    """
    if source.name.lower() == wav.name.lower() and source.parent == wav.parent:
        return False
    return source.stem.lower() == "source" or source.name.lower() == "source"


def _overall(stage: str, frac: float) -> float:
    if stage in _TERMINAL:
        return 1.0
    if stage not in STAGE_WEIGHTS:
        return 0.0
    before = sum(STAGE_WEIGHTS[s] for s in _ORDER[:_ORDER.index(stage)])
    return min(1.0, before + STAGE_WEIGHTS[stage] * max(0.0, min(1.0, frac)))


class TranscriptionQueue:
    def __init__(self, max_concurrent: int = 2) -> None:
        self.max_concurrent = max_concurrent
        self._records: dict[str, dict[str, Any]] = {}
        self._queue: deque[str] = deque()
        self._running: set[str] = set()
        self._cancel: set[str] = set()
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ API
    def submit(self, project_id: str, source: str, options: dict[str, Any]) -> dict[str, Any]:
        with self._guard:
            existing = self._records.get(project_id)
            if existing and existing["state"] in (QUEUED, RUNNING):
                return dict(existing)
            self._cancel.discard(project_id)
            self._records[project_id] = {
                "project": project_id,
                "state": QUEUED,
                "stage": "queued",
                "stage_label": STAGE_LABELS["queued"],
                "progress": 0.0,
                "detail": "等待中",
                "error": None,
                "eta": None,
                "source": source,
                "options": options,
                "submitted_at": time.time(),
                "started_at": None,
                "stage_started_at": None,
                "work_started_at": None,
                "finished_at": None,
                "elapsed": 0,
                "audio_seconds": 0,
            }
            self._queue.append(project_id)
            log.info("專案 %s 排入辨識佇列（前面 %d 個）", project_id, len(self._queue) - 1)
            self._pump_locked()
            return dict(self._records[project_id])

    def cancel(self, project_id: str) -> bool:
        with self._guard:
            record = self._records.get(project_id)
            if not record or record["state"] not in (QUEUED, RUNNING):
                return False
            self._cancel.add(project_id)
            if record["state"] == QUEUED:
                try:
                    self._queue.remove(project_id)
                except ValueError:
                    pass
                self._finish_locked(project_id, CANCELLED, "cancelled", "已取消")
                self._pump_locked()
            else:
                record["detail"] = "取消中…"
        store.set_status(project_id, "cancelled")
        log.info("專案 %s 取消辨識", project_id)
        return True

    def status(self, project_id: str) -> dict[str, Any] | None:
        with self._guard:
            record = self._records.get(project_id)
            if not record:
                return None
            return self._public_locked(record)

    def all_status(self) -> dict[str, dict[str, Any]]:
        with self._guard:
            return {k: self._public_locked(r) for k, r in self._records.items()}

    def active_count(self) -> int:
        with self._guard:
            return sum(1 for r in self._records.values() if r["state"] in (QUEUED, RUNNING))

    def forget(self, project_id: str) -> None:
        """Drop a record - used when its project is deleted.

        A running worker keeps its slot until it actually stops; releasing it
        here would over-subscribe. The worker's finish path frees the slot even
        though its record is already gone.
        """
        with self._guard:
            was_running = project_id in self._running
            if was_running:
                self._cancel.add(project_id)      # ask it to wind down
            self._records.pop(project_id, None)
            try:
                self._queue.remove(project_id)
            except ValueError:
                pass
            if not was_running:
                self._cancel.discard(project_id)
                self._pump_locked()

    # -------------------------------------------------------------- internals
    def _public_locked(self, record: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in record.items() if k not in ("source", "options")}
        out["position"] = self._position_locked(record["project"])
        return out

    def _position_locked(self, project_id: str) -> int:
        record = self._records.get(project_id)
        if not record:
            return -1
        if record["state"] == RUNNING:
            return 0
        if record["state"] == QUEUED:
            try:
                return self._queue.index(project_id) + 1
            except ValueError:
                return -1
        return -1

    def _finish_locked(self, project_id: str, state: str, stage: str,
                       detail: str, error: str | None = None) -> None:
        # Free the concurrency slot FIRST. If the project was deleted mid-run its
        # record is already gone, and returning early used to strand the slot -
        # after a couple of those the queue stops starting anything at all.
        self._running.discard(project_id)
        self._cancel.discard(project_id)
        record = self._records.get(project_id)
        if not record:
            return
        record.update(state=state, stage=stage, stage_label=STAGE_LABELS.get(stage, stage),
                      detail=detail, error=error, finished_at=time.time(), eta=None)
        if state == DONE:
            record["progress"] = 1.0

    def _pump_locked(self) -> None:
        # Scheduling is by CPU only. Memory is the operating system's job: with
        # a page file behind it, an idle model's pages are written out and cost
        # nothing. Measured while two big jobs ran on a 32 GB box with a 49 GB
        # SSD page file: 16 pages/sec and 3 MB/s of disk - CPU-bound, not
        # swapping. Holding work back because free RAM looked low was wrong.
        while self._queue and len(self._running) < self.max_concurrent:
            project_id = self._queue.popleft()
            record = self._records.get(project_id)
            if not record or record["state"] != QUEUED:
                continue
            record.update(state=RUNNING, stage="decode",
                          stage_label=STAGE_LABELS["decode"],
                          detail="準備中…", started_at=time.time())
            self._running.add(project_id)
            threading.Thread(target=self._worker, args=(project_id,),
                             name=f"transcribe-{project_id}", daemon=True).start()

    def _set(self, project_id: str, stage: str, frac: float, detail: str) -> None:
        now = time.time()
        with self._guard:
            record = self._records.get(project_id)
            if not record:
                return

            if stage != record.get("stage"):
                record["stage_started_at"] = now
            record["stage"] = stage
            record["stage_label"] = STAGE_LABELS.get(stage, stage)
            record["progress"] = round(_overall(stage, frac), 4)
            record["detail"] = detail
            record["elapsed"] = round(now - (record.get("started_at") or now))

            # Decode and model-load are near-fixed costs; they do not scale with
            # the recording. Extrapolating the whole job from them produced a
            # confident 2-minute estimate for a 25-minute file. Time only the
            # stages whose cost is proportional to the audio, and say nothing
            # until enough of that work has actually happened.
            if stage in _PROPORTIONAL:
                if record.get("work_started_at") is None:
                    record["work_started_at"] = now
                done = (record["progress"] - _FIXED_WEIGHT) / (1 - _FIXED_WEIGHT)
                work_elapsed = now - record["work_started_at"]
                if done >= 0.05 and work_elapsed >= 15:
                    record["eta"] = round(work_elapsed * (1 - done) / done)
                else:
                    record["eta"] = None            # UI shows 估算中…
            else:
                record["eta"] = None

    def _cancelled(self, project_id: str) -> bool:
        with self._guard:
            return project_id in self._cancel

    def _worker(self, project_id: str) -> None:
        from pathlib import Path

        record = self._records.get(project_id)
        if not record:
            return
        source = Path(record["source"])
        opts = record["options"]
        model = opts["model"]
        started = time.time()
        store.set_status(project_id, "processing")

        try:
            # A model that is still downloading is waited on rather than failing.
            if not is_downloaded(model):
                label = ASR_MODELS[model]["label"]
                self._set(project_id, "wait_model", 0.0, f"{label} 下載中…")
                if not downloads.manager.is_active(model):
                    downloads.manager.enqueue(model)

                # Finished download records expire, so "no record" is ambiguous:
                # it can mean finished-and-forgotten, or never-started. Re-check
                # and re-queue a bounded number of times instead of spinning here
                # forever holding a slot.
                restarts = 0
                while not is_downloaded(model):
                    if self._cancelled(project_id):
                        raise _Cancelled()

                    state = downloads.manager.status(model)
                    if state and state["state"] in ("error", "cancelled"):
                        raise RuntimeError(
                            f"{label} 下載未完成："
                            f"{state.get('error') or state['state_label']}"
                        )
                    if state:
                        self._set(project_id, "wait_model", 0.0,
                                  f"{state['state_label']} {state['progress'] * 100:.0f}%")
                    elif not downloads.manager.is_active(model):
                        if restarts >= 2:
                            raise RuntimeError(
                                f"{label} 下載沒有完成，已重試 {restarts} 次。"
                                f"請到「模型設定」手動下載後再重新辨識。"
                            )
                        restarts += 1
                        log.warning("模型 %s 的下載不在佇列中，重新排入（第 %d 次）",
                                    model, restarts)
                        downloads.manager.enqueue(model)
                    time.sleep(0.8)

            self._set(project_id, "decode", 0.0, "讀取音檔…")
            try:
                samples = load_16k_mono(source)
            except AudioError as exc:
                raise RuntimeError(str(exc)) from exc
            seconds = duration_of(samples)
            self._set(project_id, "decode", 1.0, f"{seconds:.0f} 秒")
            if self._cancelled(project_id):
                raise _Cancelled()

            store.set_duration(project_id, seconds)
            wav = store.project_dir(project_id) / "audio.wav"
            with self._guard:
                if project_id in self._records:
                    self._records[project_id]["audio_seconds"] = round(seconds)
            from .audio import write_wav
            write_wav(wav, samples)

            # Write each decoded batch straight to the project so the browser
            # can show a long meeting filling in, instead of a placeholder for
            # several minutes. The cluster list is captured on the way past:
            # the final populate() needs it too, or the speaker numbering the
            # user has been watching changes at the last moment.
            seen_clusters: list[int] = []

            def _partial(utts: list, clusters: list[int]) -> None:
                seen_clusters[:] = clusters
                store.populate_progress(project_id, utts, clusters)

            utterances = asr.transcribe(
                samples, model,
                num_speakers=opts.get("num_speakers", -1),
                zh_mode=opts.get("zh_mode"),
                language=opts.get("language", ""),
                num_threads=opts.get("threads", 4),
                progress=lambda s, f, d: self._set(project_id, s, f, d),
                cancelled=lambda: self._cancelled(project_id),
                wav_path=str(wav),
                on_partial=_partial,
            )
            if self._cancelled(project_id):
                raise _Cancelled()

            self._set(project_id, "save", 1.0, "儲存結果…")
            del samples                  # a 25-minute recording is ~100 MB
            # Same cluster list as the partial writes used, so the speaker
            # numbering does not shift when the final version lands.
            store.populate(project_id, utterances, seen_clusters or None)
            # Not an unconditional unlink: a re-run is handed audio.wav as its
            # source, so this used to delete the project's only recording.
            if disposable_upload(source, wav):
                source.unlink(missing_ok=True)

        except _Cancelled:
            with self._guard:
                self._finish_locked(project_id, CANCELLED, "cancelled", "已取消")
                self._pump_locked()
            store.set_status(project_id, "cancelled")
            log.info("專案 %s 已取消", project_id)
            return
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.exception("專案 %s 辨識失敗", project_id)
            with self._guard:
                self._finish_locked(project_id, ERROR, "error", "辨識失敗", message)
                self._pump_locked()
            store.set_status(project_id, "error", message)
            return

        with self._guard:
            self._finish_locked(project_id, DONE, "done", "完成")
            self._pump_locked()
        log.info("專案 %s 完成（耗時 %.0f 秒）", project_id, time.time() - started)


class _Cancelled(Exception):
    pass


# Sized from the machine's core count in config.py: jobs x threads should fill
# the box without starving the OS. Override with SCRIBE_JOBS / SCRIBE_THREADS.
manager = TranscriptionQueue(max_concurrent=MAX_CONCURRENT_JOBS)

