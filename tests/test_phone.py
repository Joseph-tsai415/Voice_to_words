"""Sending a recording from a phone by scanning a QR code.

The laptop shows a QR code in the 新增會議 dialog; the phone opens it, picks a
file, and the file lands on the laptop ready to start. Four things here must
hold, and each one failing would be quiet:

* The phone has to be handed an address it can actually reach. With the
  Windows hotspot on there is still a default route through the office
  Wi-Fi, so "the default route" alone would print the very address the user
  just switched away from.
* One QR code serves one phone. A colleague photographing the screen must
  get a refusal, not an upload slot.
* What the phone sends is held until it is used, a half transfer never shows
  up as a file, and the phone's filename never reaches the filesystem.
* The listener the phone talks to serves nothing but the phone page, and
  only exists while the dialog is open. The main app stays on 127.0.0.1.

Run:  .venv/Scripts/python.exe tests/test_phone.py
"""
from __future__ import annotations

import io
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import audio, incoming, lanaddr, phone   # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


def test_pick_address() -> None:
    section("選手機連得到的位址")
    check("熱點開著就用熱點",
          lanaddr.pick(["127.0.0.1", "192.168.0.128", "192.168.137.1"], "192.168.0.128")
          == "192.168.137.1")
    check("否則用預設路由",
          lanaddr.pick(["192.168.0.128", "172.24.80.1"], "192.168.0.128") == "192.168.0.128")
    check("WSL 的 172.x 排最後",
          lanaddr.pick(["172.24.80.1", "192.168.0.128"], None) == "192.168.0.128")
    check("10.x 優先於 172.16/12",
          lanaddr.pick(["172.24.80.1", "10.0.0.5"], None) == "10.0.0.5")
    check("loopback / link-local 不算",
          lanaddr.pick(["127.0.0.1", "169.254.10.2"], None) is None)
    check("公網、CGNAT（Tailscale）不算",
          lanaddr.pick(["8.8.4.4", "100.101.1.2"], None) is None)
    check("清單是空的也接受預設路由",
          lanaddr.pick([], "192.168.1.9") == "192.168.1.9")
    check("預設路由是 VPN 的 100.x 不採用",
          lanaddr.pick(["192.168.0.128"], "100.101.1.2") == "192.168.0.128")


def _raises(exc: type[BaseException], fn, *args) -> bool:
    try:
        fn(*args)
    except exc:
        return True
    return False


def test_incoming(tmp: str) -> None:
    section("手機傳來的檔案先放著")
    item = incoming.receive(io.BytesIO(b"a" * 3_000_000), "孟青宴安討論.m4a", 3_000_000)
    check("中文檔名留著",
          item["filename"] == "孟青宴安討論.m4a" and item["name"] == "孟青宴安討論",
          f"{item['filename']!r} {item['name']!r}")
    check("檔案寫到 file.m4a，不用原檔名",
          (incoming.INCOMING_DIR / item["id"] / "file.m4a").stat().st_size == 3_000_000)
    again = incoming.receive(io.BytesIO(b"b" * 10), "孟青宴安討論.m4a", 10)
    check("同名傳兩次是兩個檔案",
          again["id"] != item["id"] and len(incoming.items()) == 2)
    try:
        incoming.receive(io.BytesIO(b"x" * 10), "筆記.txt", 10)
        check("不支援的格式要拒絕", False)
    except incoming.UnsupportedFile as e:
        check("不支援的格式要拒絕，訊息跟電腦上傳一樣",
              str(e) == audio.unsupported_reason(".txt"), str(e))
    try:
        incoming.receive(io.BytesIO(b"x" * 10), "短.m4a", 100)
        check("沒傳完要丟掉", False)
    except incoming.IncompleteUpload:
        check("沒傳完不會出現在清單",
              all(i["filename"] != "短.m4a" for i in incoming.items()))
        check("也不留 .part", not list(incoming.INCOMING_DIR.glob("*/*.part")))
    dest = Path(tmp) / "source.m4a"
    incoming.take(item["id"], dest)
    check("取用＝搬走",
          dest.stat().st_size == 3_000_000 and incoming.get(item["id"]) is None)
    check("取用後資料夾也清掉", not (incoming.INCOMING_DIR / item["id"]).exists())
    check("路徑穿越的 id 不能取",
          _raises(FileNotFoundError, incoming.take, "../projects", dest))
    check("路徑穿越的 id 不能刪", incoming.discard("..\\x") is False)
    check("可以丟掉", incoming.discard(again["id"]) and incoming.items() == [])
    stray = incoming.INCOMING_DIR / "0123456789ab"
    stray.mkdir()
    (stray / "file.m4a.part").write_bytes(b"x")
    check("啟動時清掉沒傳完的", incoming.cleanup_partials() == 1 and not stray.exists())


IPHONE_SAFARI = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
                 "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
IPHONE_LINE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Mobile/15E148 Safari Line/14.1.0")


def test_sessions() -> None:
    section("一個 QR 碼只給一台手機")
    t = [1000.0]
    phone.clock = lambda: t[0]
    s = phone.sessions
    tok = s.start()
    verdict, cookie = s.claim(tok, None, IPHONE_SAFARI)
    check("第一台手機拿到", verdict == "ok" and bool(cookie))
    check("同一台再開也可以",
          s.claim(tok, cookie, "")[0] == "ok" and s.authorize(tok, cookie))
    check("別台手機被拒",
          s.claim(tok, None, "")[0] == "other" and not s.authorize(tok, "forged"))
    check("看得出是什麼裝置", s.status()["device"] == "iPhone · Safari", s.status()["device"])
    check("LINE 內建瀏覽器認得出來",
          phone.device_label(IPHONE_LINE) == "iPhone · LINE", phone.device_label(IPHONE_LINE))
    tok2 = s.start()
    check("換一個 QR 碼後舊的失效",
          s.claim(tok, cookie, "")[0] == "gone" and s.claim(tok2, None, "")[0] == "ok")

    section("手機傳完之前不能開始")
    s.announce(3)
    check("宣告 3 個就等 3 個",
          s.status()["outstanding"] == 3 and not s.status()["ready"])
    s.end_upload(True, s.begin_upload("a.m4a", 10))
    s.end_upload(False, s.begin_upload("b.m4a", 10))
    st = s.status()
    check("成功、失敗都算傳完",
          st["batch_done"] == 2 and st["outstanding"] == 1 and not st["ready"], str(st))
    t[0] += phone.STALL_SEC + 1
    check("卡住太久就放行", s.status()["stalled"] and s.status()["ready"])

    section("電腦那邊關掉就結束")
    tok3 = s.start()
    c3 = s.claim(tok3, None, "")[1]
    running = s.begin_upload("c.m4a", 10)
    s.end()
    check("關掉後不能再傳新的", not s.authorize(tok3, c3))
    s.end_upload(True, running)
    check("傳到一半的照樣收完，計數不會亂", s.in_flight() == 0)
    tok4 = s.start()
    t[0] += phone.SESSION_IDLE_SEC + 1
    check("電腦沒在看就過期",
          s.claim(tok4, None, "")[0] == "gone" and not s.status()["active"])
    s.start()
    t[0] += phone.SESSION_IDLE_SEC - 1
    s.heartbeat()
    t[0] += phone.SESSION_IDLE_SEC - 1
    check("有在輪詢就不過期", s.status()["active"])

    section("上傳只算在開始它的那個 QR 碼")
    s.start()
    old = s.begin_upload("big.mov", 10)
    s.start()                             # 換一個 QR 碼 while big.mov is still going
    s.announce(2)
    s.end_upload(True, old)
    st = s.status()
    check("舊 QR 碼的上傳不會扣到新的這批",
          st["outstanding"] == 2 and st["batch_done"] == 0, str(st))
    check("計數照樣歸零", s.in_flight() == 0)

    section("卡住之後重傳是新的一批")
    s.start()
    s.announce(2)
    s.end_upload(True, s.begin_upload("a.m4a", 10))
    t[0] += phone.STALL_SEC + 1           # the second file never reached the server
    s.announce(1)                         # 重試
    st = s.status()
    check("重試算新的一批，不會卡在 n/n+1",
          st["outstanding"] == 1 and st["batch_total"] == 1 and st["batch_done"] == 0
          and not st["stalled"], str(st))
    phone.clock = time.monotonic          # later sections use the real clock


def _rows(s) -> list[tuple]:
    return [(f["name"], f["size"], f["bytes"], f["state"]) for f in s.status()["files"]]


def test_progress_on_laptop() -> None:
    section("手機一選好，電腦就看得到每個檔案")
    s = phone.sessions
    s.start()
    s.announce(2, [{"name": "孟青宴安討論.m4a", "size": 100}, {"name": "客戶訪談.mov", "size": 2000}])
    check("檔名、大小一選好就到",
          _rows(s) == [("孟青宴安討論.m4a", 100, 0, "waiting"), ("客戶訪談.mov", 2000, 0, "waiting")],
          str(_rows(s)))
    batch = s.status()["batch"]
    up = s.begin_upload("客戶訪談.mov", 2000)        # matched by name, not order
    s.progress(800, up)
    check("傳送中看得到進度，對到正確的那一列",
          _rows(s) == [("孟青宴安討論.m4a", 100, 0, "waiting"), ("客戶訪談.mov", 2000, 800, "sending")],
          str(_rows(s)))
    s.end_upload(True, up)
    check("傳完標成已收到", _rows(s)[1] == ("客戶訪談.mov", 2000, 2000, "done"), str(_rows(s)))
    check("計數跟清單一致",
          s.status()["batch_done"] == 1 and s.status()["outstanding"] == 1
          and s.status()["batch_total"] == 2)
    s.end_upload(False, s.begin_upload("孟青宴安討論.m4a", 100))
    check("沒傳完標成失敗", _rows(s)[0][3] == "failed", str(_rows(s)))

    s.announce(1, [{"name": "孟青宴安討論.m4a", "size": 100}])     # 重試
    check("重試是新的一批：舊的列清掉，不會重複",
          _rows(s) == [("孟青宴安討論.m4a", 100, 0, "waiting")] and s.status()["batch"] != batch,
          str(_rows(s)))

    section("一批還沒傳完就重試，用回原本那一列")
    s.start()
    s.announce(2, [{"name": "a.m4a", "size": 10}, {"name": "b.m4a", "size": 10}])
    s.end_upload(False, s.begin_upload("a.m4a", 10))
    s.announce(1, [{"name": "a.m4a", "size": 10}])
    st = s.status()
    check("失敗的那列回到等待中，不多一列",
          _rows(s) == [("a.m4a", 10, 0, "waiting"), ("b.m4a", 10, 0, "waiting")]
          and st["outstanding"] == 2 and st["batch_total"] == 2 and st["batch_done"] == 0,
          f"{_rows(s)} {st['outstanding']}/{st['batch_total']}")
    s.end()


def _http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _refused(port: int) -> bool:
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        except OSError:
            return True
        time.sleep(0.1)
    return False


def test_phone_app() -> None:
    section("手機頁面")
    app = phone.create_phone_app()
    rules = {r.rule for r in app.url_map.iter_rules()}
    check("手機那邊只有這三條路，看不到會議資料",
          rules == {"/u/<token>", "/u/<token>/batch", "/u/<token>/file"}, str(rules))
    tok = phone.sessions.start()
    a, b = app.test_client(), app.test_client()
    r = a.get(f"/u/{tok}", headers={"User-Agent": IPHONE_SAFARI})
    check("第一台打得開",
          r.status_code == 200 and "傳送錄音到電腦" in r.get_data(as_text=True), str(r.status_code))
    r = b.get(f"/u/{tok}")
    check("另一台被拒",
          r.status_code == 403 and "此 QR 碼已在另一台裝置使用" in r.get_data(as_text=True))
    r = a.get("/u/nope")
    check("亂猜的 token 是 404",
          r.status_code == 404 and "此 QR 碼已失效" in r.get_data(as_text=True))
    check("別台不能宣告", b.post(f"/u/{tok}/batch", json={"count": 1}).status_code == 403)
    r = a.post(f"/u/{tok}/batch",
               json={"files": [{"name": 5}, "junk", {"name": "a.m4a", "size": "big"}, {"size": 3}]})
    check("亂寫的檔案清單不會讓伺服器出錯",
          r.status_code == 200
          and [(f["name"], f["size"]) for f in phone.sessions.status()["files"]] == [("a.m4a", None)],
          str(phone.sessions.status()["files"]))
    tok = phone.sessions.start()          # a clean session for the rest
    a, b = app.test_client(), app.test_client()
    a.get(f"/u/{tok}", headers={"User-Agent": IPHONE_SAFARI})
    a.post(f"/u/{tok}/batch", json={"files": [{"name": "孟青宴安討論.m4a", "size": 5000},
                                              {"name": "短.m4a", "size": 100}]})
    r = a.put(f"/u/{tok}/file", data=b"m" * 5000,
              headers={"X-Filename": quote("孟青宴安討論.m4a")})
    check("中文檔名經過 header 還是對的",
          r.status_code == 200 and r.get_json()["item"]["filename"] == "孟青宴安討論.m4a",
          f"{r.status_code} {r.get_data(as_text=True)[:80]}")
    # The test client recomputes Content-Length from the stream, so a header
    # cannot fake a short body; overriding the environ can, and Werkzeug then
    # raises ClientDisconnected exactly as it does when a phone locks mid-way.
    r = a.put(f"/u/{tok}/file", input_stream=io.BytesIO(b"x" * 10),
              headers={"X-Filename": quote("短.m4a")},
              environ_overrides={"CONTENT_LENGTH": "100"})
    check("沒傳完回 400", r.status_code == 400, str(r.status_code))
    check("沒傳完也算結束，按鈕會回來", phone.sessions.status()["outstanding"] == 0,
          str(phone.sessions.status()))
    r = a.put(f"/u/{tok}/file", data=b"x", headers={"X-Filename": "a.txt"})
    check("不支援的格式回 400",
          r.status_code == 400 and "不支援的格式" in r.get_json()["error"])
    check("別台不能傳",
          b.put(f"/u/{tok}/file", data=b"x",
                headers={"X-Filename": "a.m4a"}).status_code == 403)
    check("QR 是 SVG",
          phone.qr_svg("http://192.168.0.2:5000/u/x").lstrip().startswith("<svg"))
    check("網址長這樣",
          phone.phone_url("192.168.0.2", 5000, "abc") == "http://192.168.0.2:5000/u/abc")


def test_listener() -> None:
    section("沒在用就不開網路埠")
    port = phone.ensure_listener("127.0.0.1")
    check("開得起來", _http_status(f"http://127.0.0.1:{port}/u/nope") == 404)
    check("再叫一次是同一個", phone.ensure_listener("127.0.0.1") == port)
    check("還有工作階段時不關",
          bool(phone.sessions.start()) and not phone.stop_listener_if_idle())
    phone.sessions.end()
    check("結束就關", phone.stop_listener_if_idle() and phone.listener_port() is None)
    check("關了就連不上", _refused(port))

    section("手機斷線不會讓網路埠一直開著")
    phone.SOCKET_TIMEOUT_SEC = 1.0        # 120 s in real use
    tok = phone.sessions.start()
    port = phone.ensure_listener("127.0.0.1")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/u/{tok}", timeout=5) as resp:
        cookie = resp.headers["Set-Cookie"].split(";", 1)[0]
    phone.sessions.announce(1)
    # Headers and a sliver of the body, then silence: the phone walked out of
    # Wi-Fi range without the connection ever being closed.
    silent = socket.create_connection(("127.0.0.1", port))
    silent.sendall(f"PUT /u/{tok}/file HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\n"
                   "X-Filename: a.m4a\r\nContent-Length: 1000\r\n\r\n".encode() + b"x" * 10)
    deadline = time.time() + 6
    while time.time() < deadline and phone.sessions.in_flight() == 0:
        time.sleep(0.05)
    while time.time() < deadline and phone.sessions.in_flight() > 0:
        time.sleep(0.1)
    check("斷線的上傳會逾時結束", phone.sessions.in_flight() == 0)
    check("逾時也算傳完，按鈕會回來", phone.sessions.status()["outstanding"] == 0,
          str(phone.sessions.status()))
    check("不留 .part", not list(incoming.INCOMING_DIR.glob("*/*.part")))
    phone.sessions.end()
    check("沒有卡住的上傳，網路埠就關得掉", phone.stop_listener_if_idle())
    silent.close()
    phone.SOCKET_TIMEOUT_SEC = 120.0


def main() -> int:
    test_pick_address()
    test_sessions()
    test_progress_on_laptop()
    with tempfile.TemporaryDirectory() as tmp:
        # Never touch the real data/ folder while testing.
        incoming.INCOMING_DIR = Path(tmp) / "incoming"
        test_incoming(tmp)
        test_phone_app()
        test_listener()

    print()
    if failures:
        print(f"  {len(failures)} 項失敗：")
        for f in failures:
            print("    - " + f)
        return 1
    print("  全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
