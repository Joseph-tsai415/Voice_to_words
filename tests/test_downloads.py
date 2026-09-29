"""Download-queue and model-completeness checks.

These are the paths behind the "model said it was ready but wasn't" bug and the
"clicking download twice starts two downloads" bug. No real bytes are fetched -
hub.download_model is replaced with a controllable fake.

Run:  .venv/Scripts/python.exe tests/test_downloads.py
"""
from __future__ import annotations

import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, downloads, hub          # noqa: E402

PASS, FAIL = "  ✓", "  ✗"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print((PASS if ok else FAIL) + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def touch(path: Path, size: int = 8) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


# Model directories that already existed when the test started. These hold real
# downloads - Cohere alone is 2.9 GB and takes ~7 minutes to fetch - and this
# test must never delete them. An earlier version did exactly that.
def _has_real_files(key: str) -> bool:
    folder = config.model_dir(key)
    if not folder.exists():
        return False
    # A leftover empty .part is not a real download.
    return any(f.is_file() and f.stat().st_size > 0 and f.suffix != ".part"
               for f in folder.rglob("*"))


PRE_EXISTING = {key for key in config.ASR_MODELS if _has_real_files(key)}


def scratch_model_dir(key: str) -> Path | None:
    """The directory to scribble in, or None if a real download lives there."""
    return None if key in PRE_EXISTING else config.model_dir(key)


def cleanup_scratch(key: str) -> None:
    if key in PRE_EXISTING:
        return                      # not ours - leave it alone
    shutil.rmtree(config.model_dir(key), ignore_errors=True)


# ---------------------------------------------------------------------------
def test_completeness() -> None:
    print("\n模型完整性判斷")
    if PRE_EXISTING:
        print(f"  （磁碟上已有真實下載：{', '.join(sorted(PRE_EXISTING))}；"
              f"這些會被略過，不會刪除）")

    # cohere: weights live in a 2.7 GB external-data blob listed under extra_files.
    key = "cohere-transcribe"
    folder = scratch_model_dir(key)
    if folder is None:
        check("外部 .data 未下載時不算完成", True, f"略過（{key} 已下載）")
    else:
        shutil.rmtree(folder, ignore_errors=True)
        for role_file in ("encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"):
            touch(folder / role_file)
        check("外部 .data 未下載時不算完成", not config.is_downloaded(key),
              f"missing={config.missing_files(key)}")
        touch(folder / "encoder.int8.onnx.data")
        check("補上 .data 後算完成", config.is_downloaded(key))
        shutil.rmtree(folder, ignore_errors=True)

    # qwen3: the tokenizer role is a directory; its contents are the real files.
    key = "qwen3-asr"
    folder = scratch_model_dir(key)
    if folder is None:
        check("tokenizer 目錄是空的時不算完成", True, f"略過（{key} 已下載）")
    else:
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "tokenizer").mkdir(parents=True, exist_ok=True)
        for role_file in ("conv_frontend.onnx", "encoder.int8.onnx", "decoder.int8.onnx"):
            touch(folder / role_file)
        check("tokenizer 目錄是空的時不算完成", not config.is_downloaded(key),
              f"missing={len(config.missing_files(key))} 個")
        for name in ("merges.txt", "tokenizer_config.json", "vocab.json"):
            touch(folder / "tokenizer" / name)
        check("tokenizer 檔案補齊後算完成", config.is_downloaded(key))
        shutil.rmtree(folder, ignore_errors=True)

    # A zero-length file is a truncated download, not a finished one.
    key = "paraformer-zh"
    folder = scratch_model_dir(key)
    if folder is None:
        check("0 位元組的檔案視為未完成", True, f"略過（{key} 已下載）")
    else:
        shutil.rmtree(folder, ignore_errors=True)
        touch(folder / "model.int8.onnx", size=0)
        touch(folder / "tokens.txt")
        check("0 位元組的檔案視為未完成", not config.is_downloaded(key),
              f"missing={config.missing_files(key)}")
        shutil.rmtree(folder, ignore_errors=True)

    check("內建模型仍為完成", config.is_downloaded("sense-voice"))


# ---------------------------------------------------------------------------
class FakeDownload:
    """Stands in for hub.download_model: slow, cancellable, countable."""

    def __init__(self, seconds: float = 1.2) -> None:
        self.seconds = seconds
        self.starts: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.fail_keys: set[str] = set()
        self._guard = threading.Lock()

    def __call__(self, key, progress=None, cancelled=None):
        with self._guard:
            self.starts.append(key)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            steps = 12
            for i in range(steps):
                if cancelled and cancelled():
                    raise hub.DownloadCancelled()
                if progress:
                    progress("download", (i + 1) / steps, f"{key} 假裝下載中")
                time.sleep(self.seconds / steps)
            if key in self.fail_keys:
                raise RuntimeError("模擬的下載錯誤")
            return config.model_dir(key)
        finally:
            with self._guard:
                self.concurrent -= 1


# Queue behaviour must not depend on what happens to be on this machine's disk.
# enqueue() short-circuits for a model that is already complete, so the queue
# tests pretend nothing is downloaded.
QUEUE_KEYS = {"paraformer-zh", "fire-red-asr2-ctc", "qwen3-asr", "cohere-transcribe"}


def fresh_manager(fake: FakeDownload, max_concurrent: int = 1) -> downloads.DownloadManager:
    downloads.hub.download_model = fake                     # type: ignore[assignment]
    downloads.is_downloaded = lambda key: key not in QUEUE_KEYS   # type: ignore
    return downloads.DownloadManager(max_concurrent=max_concurrent)


def wait_until(predicate, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_queue() -> None:
    print("\n下載佇列")
    fake = FakeDownload(seconds=1.0)
    mgr = fresh_manager(fake)

    a, b, c = "paraformer-zh", "fire-red-asr2-ctc", "qwen3-asr"
    mgr.enqueue(a)
    mgr.enqueue(b)
    mgr.enqueue(c)

    check("三個一起排隊時只跑一個",
          wait_until(lambda: mgr.status(a) and mgr.status(a)["state"] == "running"),
          f"a={mgr.status(a)['state']}")
    sa, sb, sc = mgr.status(a), mgr.status(b), mgr.status(c)
    check("其餘進入排隊", sb["state"] == "queued" and sc["state"] == "queued")
    check("排隊順序正確", (sa["position"], sb["position"], sc["position"]) == (0, 1, 2),
          f"{sa['position']}, {sb['position']}, {sc['position']}")

    # 重複點下載：必須沿用同一個工作，而不是再開一個
    before = len(fake.starts)
    again = mgr.enqueue(a)
    check("重複排入不會產生第二個下載", again["state"] == "running" and len(fake.starts) == before)
    again_q = mgr.enqueue(c)
    check("重複排入排隊中的模型也不會重複", again_q["state"] == "queued"
          and [k for k in (mgr.status(c),) if k]
          and mgr.status(c)["position"] == 2)

    check("全部依序完成", wait_until(lambda: mgr.active_count() == 0, timeout=30))
    check("同時最多只有一個下載", fake.max_concurrent == 1, f"max={fake.max_concurrent}")
    check("三個都跑過", sorted(fake.starts) == sorted([a, b, c]), str(fake.starts))


def test_cancel() -> None:
    print("\n取消")
    fake = FakeDownload(seconds=2.5)
    mgr = fresh_manager(fake)

    a, b = "paraformer-zh", "fire-red-asr2-ctc"
    mgr.enqueue(a)
    mgr.enqueue(b)
    wait_until(lambda: mgr.status(a) and mgr.status(a)["state"] == "running")

    check("取消排隊中的項目", mgr.cancel(b))
    check("被取消的不會開始執行",
          wait_until(lambda: mgr.status(b) and mgr.status(b)["state"] == "cancelled"))
    check("取消排隊項目不影響執行中的", mgr.status(a)["state"] == "running")

    check("取消執行中的項目", mgr.cancel(a))
    check("執行中的確實停下",
          wait_until(lambda: mgr.status(a) and mgr.status(a)["state"] == "cancelled", timeout=10),
          f"state={mgr.status(a)['state'] if mgr.status(a) else None}")
    check("取消後佇列清空", mgr.active_count() == 0)
    check("取消不存在的項目回 False", mgr.cancel("sense-voice") is False)


def test_failure_isolation() -> None:
    print("\n失敗處理")
    fake = FakeDownload(seconds=0.6)
    fake.fail_keys = {"paraformer-zh"}
    mgr = fresh_manager(fake)

    a, b = "paraformer-zh", "fire-red-asr2-ctc"
    mgr.enqueue(a)
    mgr.enqueue(b)

    check("失敗的項目標記為 error",
          wait_until(lambda: mgr.status(a) and mgr.status(a)["state"] == "error", timeout=15))
    check("錯誤訊息有保留",
          bool(mgr.status(a) and "模擬的下載錯誤" in (mgr.status(a)["error"] or "")),
          (mgr.status(a) or {}).get("error", ""))
    check("一個失敗不會擋住後面的",
          wait_until(lambda: mgr.status(b) and mgr.status(b)["state"] == "done", timeout=15),
          f"b={(mgr.status(b) or {}).get('state')}")

    check("內建模型不可排入下載", _raises(lambda: mgr.enqueue("sense-voice"), ValueError))
    check("未知模型會報錯", _raises(lambda: mgr.enqueue("nope"), KeyError))


def _raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


def test_partial_cleanup() -> None:
    print("\n殘留檔清理")
    folder = scratch_model_dir("paraformer-zh")
    if folder is None:
        check("啟動時清掉空的 .part", True, "略過（paraformer-zh 已下載）")
        return
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    touch(folder / "model.int8.onnx.part", size=0)     # 空的 = 沒有進度，該刪
    touch(folder / "tokens.txt")

    removed = hub.cleanup_partials()
    check("啟動時清掉空的 .part", any("part" in r for r in removed), str(removed))
    check("空的 .part 檔已不存在", not (folder / "model.int8.onnx.part").exists())
    check("正常檔案不受影響", (folder / "tokens.txt").exists())
    shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------------------
class FlakyServer:
    """Local HTTP server that supports Range and drops the first N connections
    part-way through, so retry + resume can be tested without the network."""

    def __init__(self, payload: bytes, drop_first: int = 2, drop_after: int = 4096):
        import http.server
        import threading

        self.payload = payload
        self.remaining_drops = drop_first
        self.drop_after = drop_after
        self.served_ranges: list[int] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):      # keep the test output clean
                pass

            def do_GET(self):
                start = 0
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng.split("=")[1].split("-")[0])
                outer.served_ranges.append(start)

                body = outer.payload[start:]
                if start:
                    self.send_response(206)
                    self.send_header("Content-Range",
                                     f"bytes {start}-{len(outer.payload) - 1}/{len(outer.payload)}")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()

                if outer.remaining_drops > 0:
                    outer.remaining_drops -= 1
                    self.wfile.write(body[:outer.drop_after])   # truncate + hang up
                    self.close_connection = True
                    return
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/model.bin"

    def stop(self) -> None:
        self.server.shutdown()


def test_resume_and_retry() -> None:
    print("\n斷線續傳與重試")
    import os

    payload = bytes(os.urandom(300_000))
    server = FlakyServer(payload, drop_first=2, drop_after=50_000)
    dest = Path(os.environ.get("TEMP", ".")) / "scribe-dl-test" / "model.bin"
    shutil.rmtree(dest.parent, ignore_errors=True)

    saved = hub.MAX_ATTEMPTS
    try:
        hub._download_one(server.url, dest, lambda d, t: None)
        check("斷線兩次後仍能完成", dest.exists())
        check("內容與原始檔完全相同", dest.read_bytes() == payload,
              f"{dest.stat().st_size:,} / {len(payload):,} bytes")
        check("有使用 Range 續傳而非重頭下載",
              len(server.served_ranges) == 3 and server.served_ranges[1] > 0,
              f"每次請求的起始位元組 = {server.served_ranges}")
        check("完成後不留 .part", not dest.with_suffix(".bin.part").exists())

        # 一直斷線 -> 重試用盡後回報錯誤，而不是無限卡住
        forever = FlakyServer(payload, drop_first=99, drop_after=1000)
        dest2 = dest.parent / "never.bin"
        hub.MAX_ATTEMPTS = 3
        try:
            hub._download_one(forever.url, dest2, lambda d, t: None)
            check("持續斷線時回報錯誤", False, "沒有拋出例外")
        except RuntimeError as exc:
            check("持續斷線時回報錯誤", "重試" in str(exc), str(exc)[:70])
        check("失敗時保留 .part 供下次續傳", dest2.with_suffix(".bin.part").exists())
        forever.stop()

        # 取消時保留進度
        cancelling = FlakyServer(payload, drop_first=0)
        dest3 = dest.parent / "cancelled.bin"
        try:
            hub._download_one(cancelling.url, dest3, lambda d, t: None,
                              cancelled=lambda: True)
            check("取消會拋出 DownloadCancelled", False, "沒有拋出")
        except hub.DownloadCancelled:
            check("取消會拋出 DownloadCancelled", True)
        cancelling.stop()
    finally:
        hub.MAX_ATTEMPTS = saved
        server.stop()
        shutil.rmtree(dest.parent, ignore_errors=True)


def test_partial_kept() -> None:
    print("\n啟動時保留可續傳的進度")
    folder = scratch_model_dir("paraformer-zh")
    if folder is None:
        check("有進度的 .part 會保留（不重頭下載）", True, "略過（paraformer-zh 已下載）")
        return
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    touch(folder / "model.int8.onnx.part", size=5_000_000)     # 5 MB 已下載
    touch(folder / "tokens.txt.part", size=0)                  # 空的，該刪

    removed = hub.cleanup_partials()
    check("空的 .part 會被清掉", any("tokens" in r for r in removed), str(removed))
    check("有進度的 .part 會保留（不重頭下載）",
          (folder / "model.int8.onnx.part").exists())
    shutil.rmtree(folder, ignore_errors=True)


def main() -> int:
    real = hub.download_model
    real_is_downloaded = downloads.is_downloaded
    try:
        test_completeness()
        test_queue()
        test_cancel()
        test_failure_isolation()
        test_partial_cleanup()
        test_resume_and_retry()
        test_partial_kept()
    finally:
        downloads.hub.download_model = real                 # type: ignore[assignment]
        downloads.is_downloaded = real_is_downloaded        # type: ignore[assignment]
        for key in ("cohere-transcribe", "qwen3-asr", "paraformer-zh"):
            cleanup_scratch(key)

    print()
    if failures:
        print(f"  {len(failures)} 項失敗：")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  全部通過")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
