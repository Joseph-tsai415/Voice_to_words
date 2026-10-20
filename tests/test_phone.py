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
import sys
import tempfile
import time
from pathlib import Path

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
    s.begin_upload("a.m4a", 10)
    s.end_upload(True)
    s.begin_upload("b.m4a", 10)
    s.end_upload(False)
    st = s.status()
    check("成功、失敗都算傳完",
          st["batch_done"] == 2 and st["outstanding"] == 1 and not st["ready"], str(st))
    t[0] += phone.STALL_SEC + 1
    check("卡住太久就放行", s.status()["stalled"] and s.status()["ready"])

    section("電腦那邊關掉就結束")
    tok3 = s.start()
    c3 = s.claim(tok3, None, "")[1]
    s.begin_upload("c.m4a", 10)
    s.end()
    check("關掉後不能再傳新的", not s.authorize(tok3, c3))
    s.end_upload(True)
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
    phone.clock = time.monotonic          # later sections use the real clock


def main() -> int:
    test_pick_address()
    test_sessions()
    with tempfile.TemporaryDirectory() as tmp:
        # Never touch the real data/ folder while testing.
        incoming.INCOMING_DIR = Path(tmp) / "incoming"
        test_incoming(tmp)

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
