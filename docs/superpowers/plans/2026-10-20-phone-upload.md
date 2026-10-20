# Phone Upload by QR Code Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a phone on the same network scan a QR code in the 新增會議 dialog and send recordings that then appear in that dialog, ready to start.

**Architecture:** The main Flask app stays on `127.0.0.1`. Opening the 用手機傳 tab starts a second, in-process Werkzeug server on `0.0.0.0` whose Flask app has exactly three routes, all gated by a per-session token plus a one-device cookie. Phone uploads stream into `data/incoming/`. The dialog lists those files, and `POST /api/projects` turns one into a project by moving the file instead of re-uploading it.

**Tech Stack:** Python 3 / Flask / Werkzeug `make_server`, `segno` (new dependency, QR as SVG), vanilla JS, self-contained test scripts (no pytest).

**Spec:** `docs/superpowers/specs/2026-10-20-phone-upload-design.md`

## Global Constraints

- Main server binding is untouched (`127.0.0.1`). The phone app has **only** `/u/<token>`, `/u/<token>/batch`, `/u/<token>/file`, created with `static_folder=None`.
- One new dependency: `segno>=1.6` in `requirements.txt`. No CDN, nothing fetched at runtime.
- No size cap on phone uploads: phone app `MAX_CONTENT_LENGTH = None`; stream in 1 MiB chunks, never buffer the whole body.
- The phone's filename never reaches the filesystem. A held file is `data/incoming/<id>/file<suffix>` plus `meta.json`, where `<id>` is 12 lower-case hex characters and is validated against `^[0-9a-f]{12}$` before any path is built from it.
- All user-facing text is zh-Hant, including the phone page. Comments in templates/JS are zh-Hant; Python comments follow the surrounding English.
- Never rebuild a polled element's DOM. Update values in place and reconcile lists by `data-id`, per CLAUDE.md.
- Every awaited promise settles on failure. Failures go through `showError`, not `toast`.
- Run Python with `PYTHONUTF8=1`, using `.venv\Scripts\python.exe`.
- Work on branch `feat/phone-upload`. Commits use the CLAUDE.md format (Conventional subject plus grouped bullets) with **no attribution trailers**.
- **Do not stage `CLAUDE.md`.** It carries the user's own uncommitted edit (the commit-format section). Docs edits to it stay in the working tree, and the final report must mention them.
- Unsupported-format wording is shared with the laptop path and comes from `audio.unsupported_reason()`, introduced in Task 2.

Exact copy (use verbatim):

| where | text |
|---|---|
| tab labels | `從這台電腦選` · `用手機傳` |
| QR hint | `用手機相機掃描，在手機上選擇錄音檔。手機和電腦要連同一個 Wi-Fi（或連上這台電腦的行動熱點）。` |
| firewall help | `第一次使用時 Windows 會詢問是否允許 Python 存取網路，請按「允許」。` |
| isolation help | `手機掃了打不開？公司或飯店的 Wi-Fi 可能擋住裝置互連，改開這台電腦的行動熱點，讓手機連上再掃一次。` |
| laptop status | `等待手機掃描…` · `已連線：{device}` · `接收中 {done}/{total}，傳完才能開始` · `手機傳送中斷` |
| renew button | `換一個 QR 碼` |
| held-file badge | `來自手機` |
| no LAN address (409) | `找不到這台電腦的網路位址。請先連上 Wi-Fi，或開啟行動熱點。` |
| phone title / sub | `傳送錄音到電腦` · `選好檔案後會直接傳到電腦上的「新增會議」。` |
| phone button / note | `選擇檔案` · `請保持此頁開啟，傳完前不要鎖定螢幕。` |
| phone per-file | `等待中` · `傳送中 {pct}%` · `✓ 已傳到電腦` · `✗ {error}` + `重試` |
| phone all done | `✓ 已傳到電腦，請在電腦上完成設定。` |
| phone unreachable | `連不上電腦。請確認手機和電腦連在同一個 Wi-Fi，而且電腦上的對話框還開著。` |
| refused (403) | `此 QR 碼已在另一台裝置使用。請在電腦上重新產生。` |
| gone (404) | `此 QR 碼已失效，請在電腦上重新產生。` |

## Review Focus

1. **Phone locks or Safari backgrounds mid-upload** (the body arrives short). Expected: no half file is listed, no `.part` is left behind, the phone shows `✗` with `重試`, and the laptop's outstanding count drops so 開始辨識 comes back. Tests in Task 2 (`receive` short body) and Task 4 (phone app short body leaves `outstanding == 0`).
2. **Chinese filename through an HTTP header** (`孟青宴安討論.m4a`; headers are latin-1). Expected: the name survives exactly. The phone percent-encodes `X-Filename` and the server unquotes it. Test in Task 4.
3. **The same file sent twice.** Expected: two separate held items, neither overwriting the other. Test in Task 2.
4. **Laptop tab closed without closing the dialog.** Expected: the session ends after `SESSION_IDLE_SEC` and the listener stops. Tests in Task 3 (expiry via fake clock) and Task 4 (`stop_listener_if_idle` after expiry).
5. **Dialog closed, or 換一個 QR 碼 pressed, while a file is mid-transfer.** Expected: the in-flight file still lands in `incoming/` and shows next time. The old token accepts nothing new. Test in Task 3 (`end_upload` after `end()` still balances `in_flight`, and `authorize` is false).

---

### Task 1: Choose the laptop's LAN address

**Files:**
- Create: `app/lanaddr.py`
- Create: `tests/test_phone.py` (harness plus this task's section; later tasks add sections)
- Modify: `requirements.txt` (append `segno>=1.6`; installed now so later tasks just import it)

**Interfaces:**
- Produces: `lanaddr.pick(candidates: list[str], default_route: str | None) -> str | None`, `lanaddr.candidates() -> list[str]`, `lanaddr.default_route_ip() -> str | None`, `lanaddr.lan_ip() -> str | None`.

- [ ] **Step 1: Branch, and install segno**

```powershell
git switch -c feat/phone-upload
.venv\Scripts\python.exe -m pip install "segno>=1.6"
```

- [ ] **Step 2: Write the failing test.** Create `tests/test_phone.py` in the style of `tests/test_upload.py`: a module docstring explaining the feature and the run command, plus `check()`, `section()` and a `main()` that returns 1 on any failure. Add `test_pick_address()`:

```python
section("選手機連得到的位址")
check("熱點開著就用熱點", lanaddr.pick(["127.0.0.1", "192.168.0.128", "192.168.137.1"], "192.168.0.128") == "192.168.137.1")
check("否則用預設路由", lanaddr.pick(["192.168.0.128", "172.24.80.1"], "192.168.0.128") == "192.168.0.128")
check("WSL 的 172.x 排最後", lanaddr.pick(["172.24.80.1", "192.168.0.128"], None) == "192.168.0.128")
check("10.x 優先於 172.16/12", lanaddr.pick(["172.24.80.1", "10.0.0.5"], None) == "10.0.0.5")
check("loopback / link-local 不算", lanaddr.pick(["127.0.0.1", "169.254.10.2"], None) is None)
check("公網、CGNAT（Tailscale）不算", lanaddr.pick(["8.8.4.4", "100.101.1.2"], None) is None)
check("清單是空的也接受預設路由", lanaddr.pick([], "192.168.1.9") == "192.168.1.9")
check("預設路由是 VPN 的 100.x 不採用", lanaddr.pick(["192.168.0.128"], "100.101.1.2") == "192.168.0.128")
```

- [ ] **Step 3: Run it and confirm it fails**

Run: `$env:PYTHONUTF8=1; .venv\Scripts\python.exe tests\test_phone.py`
Expected: `ModuleNotFoundError: No module named 'app.lanaddr'`

- [ ] **Step 4: Implement `app/lanaddr.py`.** Only `192.168.0.0/16`, `10.0.0.0/8` and `172.16.0.0/12` are eligible. Do not use `ipaddress.is_private`, which admits CGNAT/Tailscale on some Python versions. `pick` order:
  1. the first eligible `192.168.137.x`, which is the Windows Mobile Hotspot default;
  2. `default_route`, if eligible;
  3. the first eligible candidate, ranked `192.168` > `10` > `172.16/12`.

  `candidates()` uses `socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)`. Adapter names are not available without psutil or locale-dependent `ipconfig` parsing, which is why ranking is by address. `default_route_ip()` uses a UDP socket `connect(("8.8.8.8", 80))` plus `getsockname()`, which sends no packet, and returns `None` on `OSError`.

- [ ] **Step 5: Run the test and confirm it passes**

Run: `$env:PYTHONUTF8=1; .venv\Scripts\python.exe tests\test_phone.py`
Expected: every line `✓`, ending `全部通過`.

- [ ] **Step 6: Commit** (include the spec and this plan)

```powershell
git add app/lanaddr.py tests/test_phone.py requirements.txt docs/superpowers
git commit   # feat(phone): pick the address a phone can reach
```

---

### Task 2: Held files from the phone

**Files:**
- Create: `app/incoming.py`
- Modify: `app/audio.py` (add `unsupported_reason`)
- Modify: `app/server.py:215-220` (`api_project_create` uses `unsupported_reason`)
- Test: `tests/test_phone.py` (add `test_incoming()`; point `incoming.INCOMING_DIR` at a `tempfile.TemporaryDirectory()` in `main()`)

**Interfaces:**
- Produces:
  - `audio.unsupported_reason(suffix: str) -> str | None` returns `"檔名沒有副檔名，看不出是什麼格式。支援：…"` for `""`, `"不支援的格式 {suffix}。支援：…"` for an unknown suffix, and `None` when the suffix is fine. Both messages are the exact text now inline in `api_project_create`.
  - `incoming.INCOMING_DIR: Path = DATA_DIR / "incoming"` (tests reassign it).
  - `class UnsupportedFile(ValueError)` and `class IncompleteUpload(IOError)`.
  - `incoming.receive(stream: BinaryIO, filename: str, length: int | None, on_progress: Callable[[int], None] | None = None) -> dict`. It checks the suffix via `upload_name_and_suffix` and `unsupported_reason` **before** reading any of the body, and raises `UnsupportedFile(message)` on a bad one. It writes `file<suffix>.part` in 1 MiB chunks, calling `on_progress(bytes_so_far)`. If the stream ends short of `length` it deletes the slot folder and raises `IncompleteUpload`. Otherwise it renames to `file<suffix>`, writes `meta.json`, and returns the item.
  - Item dict: `{"id": str, "name": str (display stem), "filename": str (original full name), "suffix": str, "bytes": int, "received_at": str (ISO 8601)}`.
  - `incoming.items() -> list[dict]` returns completed items only (folders with `meta.json`), oldest first.
  - `incoming.get(item_id: str) -> dict | None`.
  - `incoming.take(item_id: str, dest: Path) -> dict` does `os.replace` of the file to `dest`, removes the slot folder and returns the meta. It raises `FileNotFoundError` for an unknown, invalid or incomplete id.
  - `incoming.discard(item_id: str) -> bool`.
  - `incoming.cleanup_partials() -> int` removes slot folders that have no `meta.json`.

- [ ] **Step 1: Write the failing test**

```python
section("手機傳來的檔案先放著")
item = incoming.receive(io.BytesIO(b"a" * 3_000_000), "孟青宴安討論.m4a", 3_000_000)
check("中文檔名留著", item["filename"] == "孟青宴安討論.m4a" and item["name"] == "孟青宴安討論")
check("檔案寫到 file.m4a，不用原檔名", (incoming.INCOMING_DIR / item["id"] / "file.m4a").stat().st_size == 3_000_000)
again = incoming.receive(io.BytesIO(b"b" * 10), "孟青宴安討論.m4a", 10)
check("同名傳兩次是兩個檔案", again["id"] != item["id"] and len(incoming.items()) == 2)
try:
    incoming.receive(io.BytesIO(b"x" * 10), "筆記.txt", 10); check("不支援的格式要拒絕", False)
except incoming.UnsupportedFile as e:
    check("不支援的格式要拒絕，訊息跟電腦上傳一樣", str(e) == audio.unsupported_reason(".txt"))
try:
    incoming.receive(io.BytesIO(b"x" * 10), "短.m4a", 100); check("沒傳完要丟掉", False)
except incoming.IncompleteUpload:
    check("沒傳完不會出現在清單", all(i["filename"] != "短.m4a" for i in incoming.items()))
    check("也不留 .part", not list(incoming.INCOMING_DIR.glob("*/*.part")))
dest = Path(tmp) / "source.m4a"
incoming.take(item["id"], dest)
check("取用＝搬走", dest.stat().st_size == 3_000_000 and incoming.get(item["id"]) is None)
check("取用後資料夾也清掉", not (incoming.INCOMING_DIR / item["id"]).exists())
check("路徑穿越的 id 不能取", _raises(FileNotFoundError, incoming.take, "../projects", dest))
check("路徑穿越的 id 不能刪", incoming.discard("..\\x") is False)
check("可以丟掉", incoming.discard(again["id"]) and incoming.items() == [])
(incoming.INCOMING_DIR / "0123456789ab").mkdir(); (incoming.INCOMING_DIR / "0123456789ab" / "file.m4a.part").write_bytes(b"x")
check("啟動時清掉沒傳完的", incoming.cleanup_partials() == 1)
```

(`_raises(exc, fn, *args) -> bool` is a three-line helper in the test file.)

- [ ] **Step 2: Run it and confirm it fails.** Expected: `ImportError` for `app.incoming` / `unsupported_reason`.

- [ ] **Step 3: Implement** `audio.unsupported_reason` and `app/incoming.py`, then make `api_project_create` return `jsonify(error=reason), 400` from `unsupported_reason(suffix)` in place of its two inline checks. The behaviour is identical.

- [ ] **Step 4: Run** `tests\test_phone.py` and `tests\test_upload.py`. Expected: both end `全部通過`.

- [ ] **Step 5: Commit**: `feat(phone): hold files sent from a phone until they are used`

---

### Task 3: Phone sessions (one device, expiry, batch accounting)

**Files:**
- Create: `app/phone.py` (session logic only; Task 4 adds the server)
- Test: `tests/test_phone.py` (add `test_sessions()`; replace `phone.clock` with a fake)

**Interfaces:**
- Produces:
  - Module constants: `SESSION_IDLE_SEC = 60.0` (no laptop poll → session over), `STALL_SEC = 30.0` (outstanding files with no activity → treated as failed), `COOKIE = "scribe_dev"`, and `clock: Callable[[], float] = time.monotonic`.
  - `device_label(user_agent: str) -> str`: device `iPhone` / `iPad` / `Android` / `手機`, plus browser `LINE` (` Line/`) / `Chrome` (`CriOS` or `Chrome`) / `Safari`, joined as `"iPhone · LINE"`.
  - `class Sessions`, guarded by one `threading.Lock`, with module-level instance `sessions`. Methods:
    - `start() -> str` returns a new token (`secrets.token_urlsafe(16)`). It ends any current session, and resets the batch counters and `last_seen`.
    - `end() -> None`.
    - `heartbeat() -> bool` touches `last_seen`; returns false if there is no live session.
    - `claim(token: str, cookie: str | None, user_agent: str) -> tuple[str, str | None]` returns a verdict of `"ok"`, `"other"` or `"gone"`, plus a new cookie value only when this call made the claim. Store `sha256(cookie)`, never the cookie.
    - `authorize(token: str, cookie: str | None) -> bool`.
    - `announce(count: int) -> None` adds to the outstanding count. If nothing was outstanding, it starts a new batch (`batch_total = batch_done = 0`) first.
    - `begin_upload(name: str, total: int | None) -> None`, `progress(nbytes: int) -> None`, `end_upload(ok: bool) -> None`. `begin_upload`/`end_upload` move a module-wide `in_flight` counter that survives `end()`. `end_upload` decrements outstanding (floor 0) and increments `batch_done`.
    - `in_flight() -> int`.
    - `status() -> dict` returns `{"active", "claimed", "device", "outstanding", "batch_total", "batch_done", "receiving": {"name", "bytes", "total"} | None, "stalled", "ready"}`, where `ready = outstanding == 0 or stalled`.
  - Expiry is lazy. Every method treats a session whose `clock() - last_seen > SESSION_IDLE_SEC` as ended.

- [ ] **Step 1: Write the failing test**

```python
section("一個 QR 碼只給一台手機")
t = [1000.0]; phone.clock = lambda: t[0]
s = phone.sessions; tok = s.start()
verdict, cookie = s.claim(tok, None, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) ... Safari/604.1")
check("第一台手機拿到", verdict == "ok" and cookie)
check("同一台再開也可以", s.claim(tok, cookie, "")[0] == "ok" and s.authorize(tok, cookie))
check("別台手機被拒", s.claim(tok, None, "")[0] == "other" and not s.authorize(tok, "forged"))
check("看得出是什麼裝置", s.status()["device"] == "iPhone · Safari")
check("LINE 內建瀏覽器認得出來", phone.device_label("... iPhone ... Line/14.1.0") == "iPhone · LINE")
tok2 = s.start()
check("換一個 QR 碼後舊的失效", s.claim(tok, cookie, "")[0] == "gone" and s.claim(tok2, None, "")[0] == "ok")

section("手機傳完之前不能開始")
s.announce(3); check("宣告 3 個就等 3 個", s.status()["outstanding"] == 3 and not s.status()["ready"])
s.begin_upload("a.m4a", 10); s.end_upload(True); s.begin_upload("b.m4a", 10); s.end_upload(False)
st = s.status(); check("成功、失敗都算傳完", st["batch_done"] == 2 and st["outstanding"] == 1 and not st["ready"])
t[0] += phone.STALL_SEC + 1
check("卡住太久就放行", s.status()["stalled"] and s.status()["ready"])

section("電腦那邊關掉就結束")
tok3 = s.start(); c3 = s.claim(tok3, None, "")[1]
s.begin_upload("c.m4a", 10); s.end()
check("關掉後不能再傳新的", not s.authorize(tok3, c3))
s.end_upload(True); check("傳到一半的照樣收完，計數不會亂", s.in_flight() == 0)
tok4 = s.start(); t[0] += phone.SESSION_IDLE_SEC + 1
check("電腦沒在看就過期", s.claim(tok4, None, "")[0] == "gone" and not s.status()["active"])
tok5 = s.start(); t[0] += phone.SESSION_IDLE_SEC - 1; s.heartbeat(); t[0] += phone.SESSION_IDLE_SEC - 1
check("有在輪詢就不過期", s.status()["active"])
phone.clock = time.monotonic          # later sections use the real clock
```

- [ ] **Step 2: Run it and confirm it fails.** Expected: `ImportError` for `app.phone`.
- [ ] **Step 3: Implement** the session part of `app/phone.py` to the interface above.
- [ ] **Step 4: Run** `tests\test_phone.py`. Expected: `全部通過`.
- [ ] **Step 5: Commit**: `feat(phone): one device per qr code, sessions that end with the dialog`

---

### Task 4: The phone listener and page

**Files:**
- Modify: `app/phone.py` (add the server, QR and URL)
- Create: `app/templates/phone.html` (standalone; inline `<style>` and `<script>`, no static route)
- Test: `tests/test_phone.py` (add `test_phone_app()` with `create_phone_app().test_client()`, and `test_listener()` on `127.0.0.1` so the test never triggers the firewall prompt)

**Interfaces:**
- Consumes: `sessions`, `COOKIE`, `incoming.receive`, `UnsupportedFile`, `IncompleteUpload`, `SUPPORTED_SUFFIXES`.
- Produces:
  - `create_phone_app() -> Flask` (`static_folder=None`, `MAX_CONTENT_LENGTH=None`, same templates folder). Routes:
    - `GET /u/<token>`: calls `claim`. On `"ok"` it renders `phone.html` with `token` and `accept=",".join(SUPPORTED_SUFFIXES)`, and sets the cookie when a new one was issued (`Path=/u/<token>`, `HttpOnly`, `SameSite=Strict`). `"other"` returns 403 and `"gone"` returns 404, each a tiny zh-Hant page carrying the refusal copy.
    - `POST /u/<token>/batch` with JSON `{"count": int}`: `authorize`, then `announce`; 403 if unauthorised.
    - `PUT /u/<token>/file` with a raw body, `X-Filename` (percent-encoded UTF-8) and `Content-Length`: `authorize`, `begin_upload`, `incoming.receive(request.stream, unquote(name), request.content_length, sessions.progress)`, then `end_upload(ok)` in a `finally`. Returns 200 `{"ok": true, "item": {...}}`. `UnsupportedFile` → 400 `{"error": msg}`. `IncompleteUpload`, or any read error (Werkzeug's `ClientDisconnected`), → 400 `{"error": "沒有傳完，請重試。"}`.
  - `ensure_listener(host: str = "0.0.0.0") -> int` starts `werkzeug.serving.make_server(host, 0, create_phone_app(), threaded=True)` on a daemon thread if none is running, and returns the port. It also starts a reaper thread that, every `REAP_SEC = 5.0`, calls `sessions.status()` (for lazy expiry) then `stop_listener_if_idle()`, and exits when the listener is gone.
  - `stop_listener_if_idle() -> bool` calls `shutdown()` and `server_close()` only when there is no live session **and** `in_flight() == 0`. Returns whether it stopped.
  - `listener_port() -> int | None`.
  - `phone_url(host: str, port: int, token: str) -> str` returns `f"http://{host}:{port}/u/{token}"`.
  - `qr_svg(url: str) -> str` via `segno.make_qr(url, error="m")` rendered as inline SVG (`svg_inline(scale=6, border=2)`; check the method name against the installed segno).
- Phone page behaviour (`phone.html`; copy from Global Constraints; colours from `app.css` tokens, light and dark):
  - One big `<label>` button wrapping `<input type="file" multiple accept="{{ accept }}">`.
  - On change, reject unsupported suffixes on the phone with the same wording, then `POST batch {count}`, then upload one file after another with `XMLHttpRequest`, `PUT`, `xhr.send(file)` and `X-Filename: encodeURIComponent(file.name)`. Each file gets a row with a progress bar.
  - A failed file shows `✗ {error}` and `重試`. Retry announces `{count: 1}` and re-sends. A network error (`xhr.onerror`) uses the unreachable copy.
  - When everything has succeeded, show the all-done line. The button stays usable for more files.

- [ ] **Step 1: Write the failing tests**

```python
section("手機頁面")
app = phone.create_phone_app()
rules = {r.rule for r in app.url_map.iter_rules()}
check("手機那邊只有這三條路，看不到會議資料", rules == {"/u/<token>", "/u/<token>/batch", "/u/<token>/file"}, str(rules))
tok = phone.sessions.start(); a, b = app.test_client(), app.test_client()
r = a.get(f"/u/{tok}", headers={"User-Agent": "iPhone Safari"})
check("第一台打得開", r.status_code == 200 and "傳送錄音到電腦" in r.get_data(as_text=True))
check("另一台被拒", b.get(f"/u/{tok}").status_code == 403)
check("亂猜的 token 是 404", a.get("/u/nope").status_code == 404)
check("別台不能宣告", b.post(f"/u/{tok}/batch", json={"count": 1}).status_code == 403)
a.post(f"/u/{tok}/batch", json={"count": 2})
r = a.put(f"/u/{tok}/file", data=b"m" * 5000, headers={"X-Filename": quote("孟青宴安討論.m4a")})
check("中文檔名經過 header 還是對的", r.status_code == 200 and r.get_json()["item"]["filename"] == "孟青宴安討論.m4a")
r = a.put(f"/u/{tok}/file", input_stream=io.BytesIO(b"x" * 10), headers={"X-Filename": "短.m4a", "Content-Length": "100"})
check("沒傳完回 400", r.status_code == 400)
check("沒傳完也算結束，按鈕會回來", phone.sessions.status()["outstanding"] == 0)
r = a.put(f"/u/{tok}/file", data=b"x", headers={"X-Filename": "a.txt"})
check("不支援的格式回 400", r.status_code == 400 and "不支援的格式" in r.get_json()["error"])
check("別台不能傳", b.put(f"/u/{tok}/file", data=b"x", headers={"X-Filename": "a.m4a"}).status_code == 403)
check("QR 是 SVG", phone.qr_svg("http://192.168.0.2:5000/u/x").lstrip().startswith("<svg"))

section("沒在用就不開網路埠")
port = phone.ensure_listener("127.0.0.1")
check("開得起來", _http_status(f"http://127.0.0.1:{port}/u/nope") == 404)
check("還有工作階段時不關", phone.sessions.start() and not phone.stop_listener_if_idle())
phone.sessions.end(); check("結束就關", phone.stop_listener_if_idle() and phone.listener_port() is None)
check("關了就連不上", _refused(port))
```

(`_http_status` and `_refused` are small `urllib`/`socket` helpers in the test file.)

- [ ] **Step 2: Run it and confirm it fails.** Expected: `AttributeError: module 'app.phone' has no attribute 'create_phone_app'`.
- [ ] **Step 3: Implement** the server half of `app/phone.py` and `app/templates/phone.html`.
- [ ] **Step 4: Run** `tests\test_phone.py`. Expected: `全部通過`.
- [ ] **Step 5: Commit**: `feat(phone): phone page and a listener that only exists while in use`

---

### Task 5: Main-app API and starting a project from a held file

**Files:**
- Modify: `app/server.py`: add the routes below, and in `create_app()` call `incoming.cleanup_partials()`, logging the count when it is non-zero. Refactor `api_project_create`: extract `_queue_new_project(display_name: str, suffix: str, place: Callable[[Path], None]) -> Response` from its body after validation, where `place(dest)` puts the audio at `source<suffix>`.
- Modify: `tests/test_e2e.py`: give `request()` a `form: bool = False` that forces multipart without files, and add a 手機傳檔 section before the final cleanup.

**Interfaces:**
- Consumes: `lanaddr.lan_ip`, `phone.ensure_listener`, `phone.stop_listener_if_idle`, `phone.sessions`, `phone.phone_url`, `phone.qr_svg`, `incoming.items`, `incoming.get`, `incoming.take`, `incoming.discard`.
- Produces (used by Task 6):
  - `POST /api/phone/session` returns `{...sessions.status(), "url": str, "qr_svg": str}`, or 409 `{"error": <no-LAN copy>}` when `lan_ip()` is `None`. Each call starts a fresh session; this is also 換一個 QR 碼.
  - `GET /api/phone/session` returns `{...sessions.status(), "incoming": incoming.items()}`. It calls `heartbeat()`, and the dialog polls it every second.
  - `DELETE /api/phone/session` calls `end()` then `stop_listener_if_idle()`, and returns `{"ok": true}`.
  - `GET /api/incoming` returns `{"items": incoming.items()}`.
  - `DELETE /api/incoming/<item_id>` returns `{"ok": true}`, or 404 when `discard` returns False.
  - `POST /api/projects` with form field `incoming=<item_id>` instead of `audio`. It does `incoming.get()` (404 `{"error": "這個檔案已經不在了，可能已經用過或刪掉。"}`), takes the name and suffix from the item, re-checks `unsupported_reason`, then `_queue_new_project(..., place=lambda d: incoming.take(item_id, d))`. Every other form field means exactly what it means for an upload.

- [ ] **Step 1: Write the failing e2e section.** It sits after the speaker/edit checks and before the cleanup loop, and uses a separate project so the main flow is untouched.
  1. `POST /api/phone/session` returns 200 with `"<svg"` in `qr_svg`; parse port and token from `url`.
  2. Use `http://127.0.0.1:{port}`: `GET /u/{tok}` returns 200 and a `scribe_dev` cookie; a second GET without the cookie returns 403.
  3. `POST batch {"count": 1}`, then `GET /api/phone/session` reports `ready is False`.
  4. `PUT /u/{tok}/file` with the synthesised `meeting.wav` bytes, `X-Filename: quote("手機錄音.wav")` and the cookie returns 200.
  5. `GET /api/phone/session` reports `ready is True`, with one incoming item whose `filename == "手機錄音.wav"`.
  6. `POST /api/projects` (`form=True`, `{"incoming": id, "model": "sense-voice", "num_speakers": n_voices}`) returns 200 with a project id, and `GET /api/incoming` no longer lists the item.
  7. Cancel and `DELETE` that project. `DELETE /api/phone/session` returns 200. Connecting to the phone port is refused within 2 s.

  Note: the first e2e run binds `0.0.0.0` for real, so Windows shows its firewall prompt once. Click 允許, which is the same prompt a user sees.

- [ ] **Step 2: Run it and confirm it fails.** Run `tests\test_e2e.py` (nothing else loading a model at the same time). Expected: the 手機傳檔 section fails at `POST /api/phone/session` with 404.
- [ ] **Step 3: Implement** the routes, the refactor and the startup cleanup.
- [ ] **Step 4: Run** `tests\test_e2e.py`, `tests\test_phone.py` and `tests\test_upload.py`. Expected: all end `全部通過`.
- [ ] **Step 5: Commit**: `feat(phone): start a meeting from a file the phone sent`

---

### Task 6: The 用手機傳 tab in the 新增會議 dialog

**Files:**
- Modify: `app/templates/index.html:110-114` (the 錄音檔 field)
- Modify: `app/static/app.js` (`initNewDialog`, plus new phone helpers beside it)
- Modify: `app/static/app.css` (tabs, QR panel, held-file rows)

**Interfaces:**
- Consumes: the Task 5 endpoints.
- DOM ids and classes (zh-Hant comments in markup/JS):
  - Tab buttons `#src-local` and `#src-phone`.
  - Panels `#src-local-panel`, which holds the existing `#f-audio` and loses its `required` attribute, and `#src-phone-panel`. The phone panel contains `#phone-qr`, `#phone-url` (selectable text), `#phone-status`, the `#phone-renew` button and the two help lines.
  - `#incoming-list`, shown under the panels in **both** tabs whenever it is non-empty, with rows `.incoming-row[data-id]` (filename, size in MB, a `pill` `來自手機`, and a `.incoming-x` × button).
- JS functions:
  - `startPhone()`: `POST`, render the QR once, start polling. On 409, show the error text in the panel, not a dialog.
  - `stopPhone()`: stop polling, `DELETE` session, swallowing errors.
  - `pollPhone()`: `GET` every 1000 ms; updates `#phone-status`, `renderIncoming`, `updateStartGate`.
  - `renderIncoming(items)`: reconcile by `data-id`.
  - `updateStartGate()`: `#new-start.disabled = !state.phone.ready`.
- Wiring:
  - Opening the dialog fetches `GET /api/incoming`, so files held from an earlier dialog show up.
  - The `close` event on `#dlg-new` (cancel, Esc and start all close it) calls `stopPhone()`, and so does switching to `#src-local`.
  - `#phone-renew` calls `startPhone()` again.
  - `#new-start` submits local files as now plus one `FormData` per held item with `incoming=<id>` and no `audio`. A name override applies only when the total count is 1. The "no files" toast counts both kinds.
  - Status text from `status()`, in priority order: `stalled` → `手機傳送中斷`; `outstanding > 0` → `接收中 {batch_done}/{batch_total}，傳完才能開始`; `claimed` → `已連線：{device}`; otherwise `等待手機掃描…`.
  - If a poll returns `active: false` while the phone tab is showing (the laptop slept past `SESSION_IDLE_SEC`), call `startPhone()` once to show a fresh code. The server side already restarts the listener through `ensure_listener()`.

- [ ] **Step 1: Implement** the markup, styles and JS above.
- [ ] **Step 2: Verify in the browser.** Start the app (`.\run.ps1 -Port 7870`, or the `run` skill) and drive it with the Chrome DevTools tools. Each of these must hold:
  1. 用手機傳 shows an SVG QR, a `http://<lan-ip>:<port>/u/…` URL and `等待手機掃描…`.
  2. With `curl` as the phone (`curl -c jar <url>`, then `curl -b jar -X POST …/batch -H "Content-Type: application/json" -d "{\"count\":2}"`), the status becomes `接收中 0/2，傳完才能開始` and 開始辨識 is disabled.
  3. One `curl -b jar -X PUT --data-binary @tests\samples\<any small wav> -H "X-Filename: a.wav" …/file` gives `接收中 1/2` and a row `a.wav · 來自手機`. The button is still disabled.
  4. After `STALL_SEC`, the status reads `手機傳送中斷` and the button enables.
  5. Close and reopen the dialog: the row is still there and the session is gone (`GET` of the old URL returns 404). × removes the row.
  6. 換一個 QR 碼 changes the URL and the old one returns 404.
  7. 開始辨識 with a held row creates a project that appears in the sidebar and runs.
  8. The panel does not flicker during polling (QR and rows are not re-created; check in DevTools that nodes keep their identity).
- [ ] **Step 3: Run** `tests\test_phone.py` and `tests\test_e2e.py`. Expected: both `全部通過`.
- [ ] **Step 4: Commit**: `feat(phone): qr code tab in the new-meeting dialog`

---

### Task 7: Docs and the final check

**Files:**
- Modify: `CLAUDE.md`. **Leave unstaged**; see Global Constraints.

- [ ] **Step 1: CLAUDE.md**
  1. Add `tests\test_phone.py` to the Commands block, with the comment `# QR upload: one device, held files, the phone listener`, and to the "need no audio" list.
  2. Add a `## Phone upload` section covering:
     - why the phone listener is a separate server instead of binding the main app to `0.0.0.0` (a route guard is one bug from exposing every transcript; a listener with no such routes cannot);
     - the one-device-per-browser rule and the LINE case;
     - hotspot-first address choice;
     - 開始辨識 waiting on outstanding files, and `STALL_SEC`;
     - held files surviving a closed dialog;
     - the firewall prompt.
- [ ] **Step 2: Full suite.** Run every script listed in CLAUDE.md's Commands block, `test_e2e.py` last and alone. Expected: each ends `全部通過`.
- [ ] **Step 3: Hand the user the real-phone checklist**, which cannot be automated here:
  - iPhone Safari and an Android phone each scan the QR with the stock camera;
  - the picker offers Photos, Videos and Files;
  - an `.m4a` from Files and a `.mov` from Photos both arrive;
  - a second phone scanning the same code sees `此 QR 碼已在另一台裝置使用…`;
  - locking the phone mid-transfer gives `✗` + `重試` and the laptop button comes back.
- [ ] **Step 4: Report**: commits on `feat/phone-upload`, the unstaged `CLAUDE.md` edit, and the checklist.
