"""End-to-end check: upload -> transcribe -> rename speakers -> fix one
sentence's speaker -> split/merge -> export.

Run:  .venv/Scripts/python.exe tests/test_e2e.py
It starts its own server on a free port and cleans up the projects it makes.
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = "  ✓", "  ✗"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print((PASS if ok else FAIL) + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(method: str, url: str, body=None, files=None, timeout: int = 180):
    if files:
        boundary = "----scribe-test-boundary"
        buf = io.BytesIO()
        for key, value in (body or {}).items():
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            buf.write(str(value).encode("utf-8") + b"\r\n")
        for key, path in files.items():
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(
                f'Content-Disposition: form-data; name="{key}"; '
                f'filename="{Path(path).name}"\r\n'.encode()
            )
            buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
            buf.write(Path(path).read_bytes() + b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        data = buf.getvalue()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        data, headers = None, {}

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    if os.environ.get("TRACE"):
        print(f"      -> {method} {url.split('/api/')[-1][:60]}", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw.decode("utf-8"))
            if ctype.startswith(("audio/", "application/octet-stream")):
                return resp.status, raw            # binary stays bytes
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


TEST_LINES = [
    "各位早安，我們今天討論第三季的預算分配。",
    "好的，行銷部門的數字總共是三百二十萬。",
    "這個數字比上一季增加了多少？",
    "大概增加了百分之十五，主要是新產品推廣費用。",
]


def _ps(script: str) -> subprocess.CompletedProcess:
    """Run a PowerShell script from a file - a multi-line -Command string gets
    mangled and silently produces empty output."""
    tmp = Path(os.environ.get("TEMP", ".")) / "scribe-e2e-ps.ps1"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(script, encoding="utf-8-sig")
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def installed_voices() -> list[str]:
    """Voice names this PowerShell host can actually select.

    Windows PowerShell 5.1 only exposes the 'Desktop' voices, so the names must
    be discovered rather than hard-coded.
    """
    proc = _ps(
        "Add-Type -AssemblyName System.Speech\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$s.GetInstalledVoices() | ForEach-Object { "
        "  if ($_.Enabled) { $_.VoiceInfo.Name + '|' + $_.VoiceInfo.Culture } }\n"
    )
    rows = [r.strip() for r in proc.stdout.splitlines() if "|" in r]
    zh = [r.split("|")[0] for r in rows if r.split("|")[1].lower().startswith("zh")]
    other = [r.split("|")[0] for r in rows if not r.split("|")[1].lower().startswith("zh")]
    return zh + other


def make_audio(dest: Path) -> tuple[Path, int]:
    """Alternating voices so diarization has something real to find.

    Returns (path, number_of_distinct_voices_used).
    """
    marker = dest.with_suffix(".voices")
    if dest.exists() and dest.stat().st_size > 100_000 and marker.exists():
        return dest, int(marker.read_text())

    dest.parent.mkdir(parents=True, exist_ok=True)
    for stale in dest.parent.glob("t*.wav"):
        stale.unlink()

    voices = installed_voices()
    if not voices:
        raise RuntimeError("系統沒有任何可用的 SAPI 語音，無法產生測試音檔。")
    chosen = voices[:2] if len(voices) >= 2 else voices
    # Different rates make two same-gender voices easier to tell apart.
    rates = [-1, 2]

    lines = ["Add-Type -AssemblyName System.Speech"]
    for i, text in enumerate(TEST_LINES):
        voice = chosen[i % len(chosen)]
        lines += [
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
            f'$s.SelectVoice("{voice}")',
            f"$s.Rate = {rates[i % len(chosen)] if len(chosen) > 1 else 0}",
            f'$s.SetOutputToWaveFile("{dest.parent / f"t{i:02d}.wav"}")',
            f'$s.Speak("{text}")',
            "$s.Dispose()",
        ]
    proc = _ps("\n".join(lines))
    if proc.returncode != 0 or "Exception" in (proc.stderr or ""):
        raise RuntimeError(f"SAPI 產生測試音檔失敗：{(proc.stderr or '')[:300]}")

    import numpy as np
    import soundfile as sf
    import soxr

    parts = []
    for wav in sorted(dest.parent.glob("t*.wav")):
        if wav.stat().st_size < 1000:
            raise RuntimeError(f"{wav.name} 是空的 — 語音 {chosen} 可能無法使用")
        x, sr = sf.read(str(wav), dtype="float32", always_2d=False)
        if x.ndim > 1:
            x = x.mean(axis=1, dtype="float32")
        if sr != 16000:
            x = soxr.resample(x, sr, 16000).astype("float32")
        parts.append(x)
        parts.append(np.zeros(int(0.55 * 16000), dtype="float32"))
    if not parts:
        raise RuntimeError("沒有產生任何測試音檔")

    sf.write(str(dest), np.concatenate(parts), 16000, subtype="PCM_16")
    marker.write_text(str(len(chosen)))
    print(f"  測試音檔語音：{', '.join(chosen)}")
    return dest, len(chosen)


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    scratch = Path(os.environ.get("TEMP", ".")) / "scribe-e2e"
    audio, n_voices = make_audio(scratch / "meeting.wav")

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    server = subprocess.Popen(
        [sys.executable, "-m", "app", "serve", "--port", str(port), "--no-browser"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace", bufsize=1,
    )

    # The pipe MUST be drained. Left unread it fills after a few KB of logging
    # and the server blocks on its next write, which looks exactly like a
    # deadlock in the app. Set SERVER_LOG=1 to echo it live.
    server_log: list[str] = []

    def drain() -> None:
        for line in server.stdout:                      # type: ignore[union-attr]
            server_log.append(line.rstrip())
            if os.environ.get("SERVER_LOG"):
                print("   [伺服器] " + line.rstrip(), flush=True)

    threading.Thread(target=drain, daemon=True).start()
    print(f"\n伺服器 {base}  (pid {server.pid})\n")

    try:
        for _ in range(60):
            try:
                if request("GET", base + "/")[0] == 200:
                    break
            except Exception:
                time.sleep(0.4)
        else:
            return check("伺服器啟動", False, "逾時") or 1

        check("伺服器啟動", True)
        status, payload = request("GET", base + "/api/models")
        models = payload.get("items", []) if isinstance(payload, dict) else []
        check("GET /api/models", status == 200 and bool(models),
              f"{len(models)} 個模型")
        check("附帶排行榜來源資訊",
              isinstance(payload, dict) and "leaderboard" in payload
              and "source" in payload["leaderboard"],
              str((payload.get("leaderboard") or {}).get("source", ""))[:60])
        from app.config import DEFAULT_MODEL
        check("預設模型已安裝可用",
              any(m["key"] == DEFAULT_MODEL and m["downloaded"] for m in models),
              DEFAULT_MODEL)
        check("模型來自 Hugging Face", any(m["repo"] for m in models))

        # ---------------------------------------------------------- 上傳
        t_upload = time.time()
        status, resp = request("POST", base + "/api/projects",
                               body={"name": "測試會議", "model": "sense-voice",
                                     "num_speakers": n_voices, "zh_mode": "s2twp",
                                     "threads": 4},
                               files={"audio": audio})
        if not check("POST /api/projects 立即回應", status == 200 and "project" in resp,
                     f"{time.time() - t_upload:.1f}s，{str(resp)[:120]}"):
            return 1
        pid = resp["project"]
        check("上傳沒有阻塞（<5 秒就回應）", time.time() - t_upload < 5,
              f"{time.time() - t_upload:.1f}s")

        # 專案立刻出現在清單裡，而且帶著自己的狀態
        status, listing = request("GET", base + "/api/projects")
        row = next((p for p in listing["items"] if p["id"] == pid), None)
        check("專案立刻出現在清單", row is not None)
        check("專案帶有進行中狀態",
              bool(row and row["status"] in ("queued", "processing")),
              (row or {}).get("status"))

        final = None
        for _ in range(300):
            _, listing = request("GET", base + "/api/projects")
            row = next((p for p in listing["items"] if p["id"] == pid), None)
            if not row:
                break
            if row["status"] == "ready":
                final = row
                break
            if row["status"] in ("error", "interrupted"):
                return check("辨識工作", False, str(row.get("error"))) or 1
            time.sleep(1)
        if not check("辨識完成", final is not None):
            return 1
        status, proj = request("GET", f"{base}/api/projects/{pid}")
        check("GET /api/projects/<id>", status == 200)
        check("有辨識出句子", len(proj["segments"]) > 0, f"{len(proj['segments'])} 句")
        check(f"有分辨出 {n_voices} 位講者", len(proj["speakers"]) == n_voices,
              f"{len(proj['speakers'])} 位")
        check("有對話框分組", len(proj.get("bubbles", [])) > 0,
              f"{len(proj.get('bubbles', []))} 組")
        check("輸出為繁體中文",
              any(ch in "們灣個這數說會議預算資產" for s in proj["segments"] for ch in s["text"]),
              proj["segments"][0]["text"][:24] if proj["segments"] else "")

        status, _ = request("GET", f"{base}/api/projects/{pid}/audio")
        check("音檔可播放", status == 200)

        # A re-run is fed audio.wav as its source, and the worker deletes its
        # source when it finishes - which used to delete the only copy of the
        # recording. Run it again and check the audio is still there.
        status, _ = request("POST", f"{base}/api/projects/{pid}/retry")
        check("可以重新辨識", status == 200, str(status))
        deadline = time.time() + 240
        state = None
        while time.time() < deadline:
            _, listing = request("GET", base + "/api/projects")
            row = next((p for p in listing["items"] if p["id"] == pid), None)
            state = row and row["status"]
            if state in ("ready", "error"):
                break
            time.sleep(1)
        check("重新辨識跑完", state == "ready", str(state))
        status, _ = request("GET", f"{base}/api/projects/{pid}/audio")
        check("重新辨識後音檔還在（不能把來源音檔刪掉）", status == 200, str(status))
        _, proj = request("GET", f"{base}/api/projects/{pid}")

        # -------------------------------------------- 講者改名 (Speaker 1 -> Joseph)
        if len(proj["speakers"]) < 2:
            _, proj = request("POST", f"{base}/api/projects/{pid}/speakers",
                              body={"name": "第二位"})
        s1, s2 = proj["speakers"][0]["id"], proj["speakers"][1]["id"]
        status, proj = request("PATCH", f"{base}/api/projects/{pid}/speakers/{s1}",
                               body={"name": "Joseph"})
        check("講者改名 -> Joseph", status == 200 and proj["speakers"][0]["name"] == "Joseph")
        status, proj = request("PATCH", f"{base}/api/projects/{pid}/speakers/{s2}",
                               body={"name": "Rebecca"})
        check("講者改名 -> Rebecca", status == 200 and proj["speakers"][1]["name"] == "Rebecca")

        # ------------------------------- 單句改講者（把聽錯的 Rebecca 改回 Joseph）
        target = next((s for s in proj["segments"] if s["speaker"] == s2),
                      proj["segments"][-1])
        status, proj = request("PATCH", f"{base}/api/projects/{pid}/segments/{target['id']}",
                               body={"speaker": s1})
        moved = next(s for s in proj["segments"] if s["id"] == target["id"])
        check("單句改講者", status == 200 and moved["speaker"] == s1)
        check("標記為已更動講者", moved["speaker_edited"] is True)

        # 改回去，方便後續檢查
        _, proj = request("PATCH", f"{base}/api/projects/{pid}/segments/{target['id']}",
                          body={"speaker": s2})

        # ------------------------------------------------------------ 改文字
        seg0 = proj["segments"][0]
        status, proj = request("PATCH", f"{base}/api/projects/{pid}/segments/{seg0['id']}",
                               body={"text": "各位早安，今天討論第三季預算。"})
        edited = next(s for s in proj["segments"] if s["id"] == seg0["id"])
        check("修改句子文字", status == 200 and edited["text"].startswith("各位早安"))
        check("標記為已編輯", edited["edited"] is True)

        # ------------------------------------------------------- 切句 / 合併
        before = len(proj["segments"])
        status, proj = request("POST",
                               f"{base}/api/projects/{pid}/segments/{seg0['id']}/split",
                               body={"at": 4})
        check("切成兩句", status == 200 and len(proj["segments"]) == before + 1,
              f"{before} -> {len(proj['segments'])}")

        status, proj = request("POST",
                               f"{base}/api/projects/{pid}/segments/{seg0['id']}/merge")
        check("合併回一句", status == 200 and len(proj["segments"]) == before,
              f"-> {len(proj['segments'])}")

        # -------------------------------------------------------- 批次改講者
        ids = [s["id"] for s in proj["segments"][:2]]
        status, proj = request("POST", f"{base}/api/projects/{pid}/segments/bulk-speaker",
                               body={"segment_ids": ids, "speaker": s1})
        ok = all(s["speaker"] == s1 for s in proj["segments"] if s["id"] in ids)
        check("批次指定講者", status == 200 and ok)

        # ------------------------------------------------------------- 新增講者
        status, proj = request("POST", f"{base}/api/projects/{pid}/speakers",
                               body={"name": "David"})
        check("新增第三位講者", status == 200 and len(proj["speakers"]) == 3)

        # ---------------------------------------------------------------- 匯出
        for fmt in ("txt", "md", "srt", "vtt", "csv", "json"):
            status, text = request("GET",
                                   f"{base}/api/projects/{pid}/export?format={fmt}&inline=1")
            body = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
            check(f"匯出 {fmt}", status == 200 and len(body) > 20, f"{len(body)} 字元")

        status, txt = request("GET", f"{base}/api/projects/{pid}/export?format=txt&inline=1")
        check("逐字稿含講者名字", "Joseph" in txt or "Rebecca" in txt,
              txt.splitlines()[2][:40] if len(txt.splitlines()) > 2 else "")

        # --------------------------------------------------- 模型 / 下載佇列 API
        status, dl = request("GET", base + "/api/downloads")
        check("GET /api/downloads", status == 200 and "items" in dl and "active" in dl,
              f"active={dl.get('active') if isinstance(dl, dict) else dl}")

        # Every catalogue entry is downloadable now. SenseVoice used to be
        # bundled with no repo and returned 400 here; setup installs X-ASR
        # instead, so an unfetchable entry would be dead on a fresh clone.
        # Re-requesting one that is already complete is a no-op, not an error.
        status, body = request("POST", base + "/api/models/sense-voice/download")
        check("已經下載好的模型再按一次是 no-op",
              status == 200 and isinstance(body, dict) and body.get("state") == "done",
              f"status={status} state={(body or {}).get('state') if isinstance(body, dict) else body}")

        status, err = request("POST", base + "/api/models/no-such-model/download")
        check("未知模型回 404", status == 404)

        # 每個模型都要回報檔案完整度，UI 才能分辨「完成」與「不完整」
        status, payload = request("GET", base + "/api/models")
        models = payload["items"]
        shaped = all("files_ready" in m and "files_total" in m and "missing" in m
                     and "busy" in m and "leaderboard" in m for m in models)
        check("模型清單帶有完整度資訊", status == 200 and shaped)
        builtin = next(m for m in models if m["key"] == "sense-voice")
        check("模型檔案數一致",
              builtin["files_ready"] == builtin["files_total"] and builtin["downloaded"],
              f"{builtin['files_ready']}/{builtin['files_total']}")

        # 用未下載的模型上傳：不再擋下，而是自動排下載並等待
        undownloaded = next((m["key"] for m in models
                             if not m["downloaded"] and not m["busy"]), None)
        if undownloaded:
            status, resp = request("POST", base + "/api/projects",
                                   body={"name": "等模型下載", "model": undownloaded},
                                   files={"audio": audio})
            check("模型未下載時上傳仍成功", status == 200 and "project" in resp,
                  f"status={status}")
            waiting_pid = resp.get("project")

            reached = False
            for _ in range(30):
                _, listing = request("GET", base + "/api/projects")
                row = next((p for p in listing["items"] if p["id"] == waiting_pid), None)
                if row and (row.get("job") or {}).get("stage") == "wait_model":
                    reached = True
                    break
                time.sleep(0.4)
            check("專案狀態顯示等待模型下載", reached,
                  ((row or {}).get("job") or {}).get("stage_label", "?"))

            # 取消下載 -> 等待中的專案必須收到錯誤，而不是永遠卡住
            request("DELETE", f"{base}/api/models/{undownloaded}/download")
            ended = False
            for _ in range(40):
                _, listing = request("GET", base + "/api/projects")
                row = next((p for p in listing["items"] if p["id"] == waiting_pid), None)
                if row and row["status"] in ("error", "cancelled"):
                    ended = True
                    break
                time.sleep(0.5)
            check("下載被取消時等待的專案不會卡住", ended,
                  (row or {}).get("status", "?"))
            request("DELETE", f"{base}/api/projects/{waiting_pid}")
        else:
            check("模型未下載時上傳仍成功", True, "（全部模型都已下載，略過）")

        # --------------------------------------------------- 多專案同時進行
        pids = []
        for i in range(3):
            status, resp = request("POST", base + "/api/projects",
                                   body={"name": f"並行測試 {i + 1}", "model": "sense-voice",
                                         "num_speakers": n_voices, "threads": 2},
                                   files={"audio": audio})
            if status == 200:
                pids.append(resp["project"])
        check("一次送出 3 個專案", len(pids) == 3, f"{len(pids)} 個")

        _, listing = request("GET", base + "/api/projects")
        rows = {p["id"]: p for p in listing["items"] if p["id"] in pids}
        states = [rows[x]["status"] for x in pids if x in rows]
        check("三個專案各自都有狀態", len(states) == 3, str(states))
        check("同時進行數受到限制", 0 < listing["active"] <= 3,
              f"active={listing['active']}")
        _, cap = request("GET", base + "/api/capacity")
        positions = [(rows[x].get("job") or {}).get("position") for x in pids if x in rows]
        running_now = sum(1 for pos in positions if pos == 0)
        check("同時執行數不超過設定上限", running_now <= cap["max_jobs"],
              f"running={running_now} 上限={cap['max_jobs']} (CPU {cap['cpu_count']} 核)")
        check("每個專案都有自己的佇列位置",
              all(pos is not None and pos >= 0 for pos in positions),
              f"positions={positions}")

        deadline = time.time() + 240
        while time.time() < deadline:
            _, listing = request("GET", base + "/api/projects")
            rows = {p["id"]: p for p in listing["items"] if p["id"] in pids}
            if all(rows.get(x, {}).get("status") == "ready" for x in pids):
                break
            time.sleep(1)
        check("三個專案全部完成",
              all(rows.get(x, {}).get("status") == "ready" for x in pids),
              str({x: rows.get(x, {}).get("status") for x in pids}))
        check("每個專案都有自己的逐字稿",
              all(rows.get(x, {}).get("num_segments", 0) > 0 for x in pids),
              str({x: rows.get(x, {}).get("num_segments") for x in pids}))
        for x in pids:
            try:
                request("DELETE", f"{base}/api/projects/{x}", timeout=30)
            except Exception as exc:            # cleanup is best-effort
                print(f"    (清理 {x} 失敗：{exc})")

        # --------------------------------------------------------- 錯誤處理
        status, _ = request("GET", base + "/api/projects/does-not-exist")
        check("找不到專案回 404", status == 404)
        status, _ = request("POST", base + "/api/projects", body={"model": "sense-voice"},
                            files={})
        check("沒有音檔回 400", status == 400)
        status, _ = request("GET", f"{base}/api/projects/{pid}/export?format=xyz")
        check("不支援格式回 400", status == 400)

        # ------------------------------------------------------------- 清理
        status, _ = request("DELETE", f"{base}/api/projects/{pid}")
        check("刪除專案", status == 200)
        status, listing = request("GET", base + "/api/projects")
        check("清單已移除", all(p["id"] != pid for p in listing["items"]))

    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print()
    if failures and server_log:
        print("  伺服器最後 20 行輸出：")
        for line in server_log[-20:]:
            print("    " + line)
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
