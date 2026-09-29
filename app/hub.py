"""Download ASR models from Hugging Face into models/hub/<key>/."""
from __future__ import annotations

import http.client
import logging
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .config import (ASR_MODELS, is_downloaded, missing_files, model_dir,
                     required_files)

log = logging.getLogger("scribe.hub")


class DownloadCancelled(Exception):
    """Raised out of a download when the caller asks it to stop."""


HF_ENDPOINT = "https://huggingface.co"
ProgressFn = Callable[[str, float, str], None]   # (stage, fraction 0-1, detail)

# One lock per model: two requests for the same model must not write the same
# .part file concurrently, which would silently corrupt it.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def is_busy(key: str) -> bool:
    """True while a download for this model holds the lock."""
    lock = _lock_for(key)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def _resolve_url(repo: str, rel_path: str) -> str:
    return f"{HF_ENDPOINT}/{repo}/resolve/main/{rel_path}?download=true"


def files_to_fetch(key: str) -> list[str]:
    """Every repo-relative file a model needs."""
    return required_files(key)


READ_TIMEOUT = 120          # seconds of silence before a connection is retried
MAX_ATTEMPTS = 5
CHUNK = 1 << 20


def _download_one(url: str, dest: Path, on_bytes: Callable[[int, int], None],
                  cancelled: Callable[[], bool] | None = None,
                  on_retry: Callable[[int, str], None] | None = None) -> None:
    """Fetch one file, resuming a partial `.part` and retrying transient faults.

    A 2.7 GB model over a flaky link will stall at some point; restarting from
    zero each time never finishes. Every attempt continues from the bytes
    already on disk using an HTTP Range request.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if cancelled and cancelled():
            raise DownloadCancelled()

        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": "meeting-scribe/1.0", "Accept-Encoding": "identity"}
        if have:
            headers["Range"] = f"bytes={have}-"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
                resuming = resp.status == 206 and have > 0
                if have and not resuming:
                    have = 0            # server ignored Range - start over
                    log.info("%s 不支援續傳，從頭下載", dest.name)

                remaining = int(resp.headers.get("Content-Length") or 0)
                total = have + remaining
                if resuming:
                    log.info("%s 從 %.0f MB 續傳（共 %.0f MB）",
                             dest.name, have / 1e6, total / 1e6)

                done = have
                with open(tmp, "ab" if resuming else "wb") as fh:
                    while True:
                        if cancelled and cancelled():
                            raise DownloadCancelled()
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        on_bytes(done, total)

            # A truncated response still leaves a readable file, which would fail
            # deep inside onnxruntime later with an unhelpful error.
            if total and done < total:
                raise TimeoutError(
                    f"連線提前結束：收到 {done:,} / {total:,} bytes")

            tmp.replace(dest)
            return

        except DownloadCancelled:
            raise                       # keep .part so a resume can continue
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and tmp.exists():
                tmp.unlink(missing_ok=True)      # bad partial; next attempt restarts
                last_error = exc
                continue
            if 400 <= exc.code < 500 and exc.code != 429:
                raise RuntimeError(
                    f"下載失敗 {dest.name} → HTTP {exc.code} {exc.reason}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException, OSError) as exc:
            last_error = exc

        if attempt < MAX_ATTEMPTS:
            got = tmp.stat().st_size if tmp.exists() else 0
            wait = min(30, 2 ** attempt)
            log.warning("%s 第 %d 次失敗（%s），%d 秒後從 %.0f MB 續傳",
                        dest.name, attempt, last_error, wait, got / 1e6)
            if on_retry:
                on_retry(attempt, f"連線中斷，{wait} 秒後重試（第 {attempt}/{MAX_ATTEMPTS - 1} 次）")
            for _ in range(wait * 4):        # stay responsive to cancel
                if cancelled and cancelled():
                    raise DownloadCancelled()
                time.sleep(0.25)

    raise RuntimeError(
        f"下載失敗 {dest.name}：重試 {MAX_ATTEMPTS} 次後仍無法完成 → {last_error}"
    )


def download_model(key: str, progress: ProgressFn | None = None,
                   cancelled: Callable[[], bool] | None = None) -> Path:
    """Fetch every missing file for `key`. Returns the model directory.

    Serialised per model: a second call for the same key waits rather than
    racing the first one onto the same temp files.
    """
    spec = ASR_MODELS.get(key)
    if spec is None:
        raise KeyError(f"未知的模型：{key}")
    if spec["repo"] is None:
        if not is_downloaded(key):
            raise RuntimeError(f"{spec['label']} 應隨專案附帶，但檔案不存在。")
        return model_dir(key)

    with _lock_for(key):
        target = model_dir(key)
        target.mkdir(parents=True, exist_ok=True)

        # Whatever is absent or zero-length; a file that finished earlier is kept.
        pending = missing_files(key)
        if not pending:
            log.info("%s 已完整，略過下載", key)
            return target

        log.info("下載 %s：%d 個檔案 %s", key, len(pending), pending)
        for idx, rel in enumerate(pending):
            def on_bytes(done: int, total: int, _rel=rel, _idx=idx) -> None:
                if progress is None:
                    return
                file_frac = done / total if total else 0.0
                overall = (_idx + file_frac) / len(pending)
                progress("download", overall,
                         f"{_rel}  {done / 1e6:.0f}/{total / 1e6:.0f} MB"
                         f"  ({_idx + 1}/{len(pending)})")

            def on_retry(attempt: int, message: str, _rel=rel, _idx=idx) -> None:
                if progress:
                    progress("download", _idx / len(pending), f"{_rel}  {message}")

            if cancelled and cancelled():
                raise DownloadCancelled()
            _download_one(_resolve_url(spec["repo"], rel), target / rel,
                          on_bytes, cancelled, on_retry)

        still = missing_files(key)
        if still:
            raise RuntimeError(f"{spec['label']} 下載後仍缺少：{', '.join(still)}")
        log.info("%s 下載完成", key)
        return target


def delete_model(key: str) -> None:
    spec = ASR_MODELS[key]
    if spec["repo"] is None:
        raise RuntimeError("內建模型不可刪除。")
    if is_busy(key):
        raise RuntimeError(f"{spec['label']} 正在下載中，無法刪除。")
    with _lock_for(key):
        shutil.rmtree(model_dir(key), ignore_errors=True)


def cleanup_partials() -> list[str]:
    """Tidy .part files at startup.

    A non-empty .part is resumable progress and is kept - re-downloading it
    would throw away gigabytes. Only empty ones are removed.
    """
    removed, resumable = [], []
    for key in ASR_MODELS:
        folder = model_dir(key)
        if not folder.exists():
            continue
        for part in folder.rglob("*.part"):
            try:
                if part.stat().st_size == 0:
                    part.unlink()
                    removed.append(str(part.relative_to(folder.parent)))
                else:
                    resumable.append((str(part.relative_to(folder.parent)),
                                      part.stat().st_size))
            except OSError as exc:
                log.warning("無法處理殘留檔 %s：%s", part, exc)
    if removed:
        log.info("已清除 %d 個空的下載殘留：%s", len(removed), removed)
    for name, size in resumable:
        log.info("發現可續傳的下載：%s（已有 %.0f MB），下次下載會接著跑", name, size / 1e6)
    return removed


def catalogue() -> list[dict]:
    """Model list for the UI, with on-disk state resolved."""
    out = []
    for key, spec in ASR_MODELS.items():
        missing = missing_files(key)
        total = len(required_files(key))
        folder = model_dir(key)
        partial_bytes = 0
        if folder.exists():
            for part in folder.rglob("*.part"):
                try:
                    partial_bytes += part.stat().st_size
                except OSError:
                    pass
        out.append({
            "key": key,
            "role": spec.get("role", "asr"),
            "label": spec["label"],
            "languages": spec["languages"],
            "size_mb": spec["size_mb"],
            "note": spec["note"],
            "arch": spec.get("arch", ""),
            "builtin": spec["repo"] is None,
            "repo": spec["repo"],
            "downloaded": not missing,
            "missing": missing,
            "files_ready": total - len(missing),
            "files_total": total,
            "partial_mb": round(partial_bytes / 1e6, 1),
        })
    return out
