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

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import lanaddr   # noqa: E402

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


def main() -> int:
    test_pick_address()

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
