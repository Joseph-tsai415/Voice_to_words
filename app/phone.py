"""Phone upload: the QR session and the listener the phone talks to.

One session exists at a time, created when the 新增會議 dialog's 用手機傳
tab opens. Its token goes in the QR code, and the first browser to open it
claims it with a cookie; anyone else who scans the same code - a colleague
photographing the screen, a screenshot passed round - is refused. Strictly
that is one *browser*, not one phone: open it in LINE's reader and then in
Safari and Safari is refused. That is the price of a guarantee that means
something, and 換一個 QR 碼 on the laptop is the way out.

The session ends when the dialog closes, or when the laptop stops polling
for SESSION_IDLE_SEC (tab closed without closing the dialog). An upload
already running is allowed to finish - it lands in incoming/ and shows the
next time the dialog opens - but nothing new is accepted.

The dialog keeps 開始辨識 disabled while files the phone announced are
still on their way, so pressing it early cannot silently leave the rest
out. A transfer that dies without the server noticing (the phone locked
before its request arrived) would hold the button forever, so after
STALL_SEC of no activity the outstanding files count as failed.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("scribe.phone")

SESSION_IDLE_SEC = 60.0        # laptop stopped polling -> session over
STALL_SEC = 30.0               # files announced, nothing moving -> treat as failed
COOKIE = "scribe_dev"
clock: Callable[[], float] = time.monotonic      # tests replace this


def device_label(user_agent: str) -> str:
    """A rough "iPhone · Safari" for the laptop to show who connected."""
    ua = user_agent or ""
    device = ("iPad" if "iPad" in ua else "iPhone" if "iPhone" in ua
              else "Android" if "Android" in ua else "手機")
    browser = ("LINE" if " Line/" in ua
               else "Chrome" if ("CriOS" in ua or "Chrome" in ua)
               else "Safari" if "Safari" in ua else "")
    return f"{device} · {browser}" if browser else device


def _hash(cookie: str) -> str:
    return hashlib.sha256(cookie.encode("utf-8")).hexdigest()


def _same(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@dataclass
class _Session:
    token: str
    last_seen: float                     # last laptop poll
    last_activity: float                 # last sign of the phone sending
    device_hash: str | None = None       # sha256 of the claiming browser's cookie
    device: str = ""
    outstanding: int = 0
    batch_total: int = 0
    batch_done: int = 0
    receiving: dict | None = None


class Sessions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._s: _Session | None = None
        self._in_flight = 0              # survives end(): an upload may outlive its session

    def _live(self) -> _Session | None:
        """The current session, expiring it first if the laptop went quiet. Lock held."""
        s = self._s
        if s is not None and clock() - s.last_seen > SESSION_IDLE_SEC:
            log.info("手機連線已過期（電腦端超過 %.0f 秒沒有回應）", SESSION_IDLE_SEC)
            self._s = s = None
        return s

    def start(self) -> str:
        with self._lock:
            now = clock()
            token = secrets.token_urlsafe(16)
            self._s = _Session(token=token, last_seen=now, last_activity=now)
            return token

    def end(self) -> None:
        with self._lock:
            self._s = None

    def heartbeat(self) -> bool:
        with self._lock:
            s = self._live()
            if s is None:
                return False
            s.last_seen = clock()
            return True

    def claim(self, token: str, cookie: str | None,
              user_agent: str) -> tuple[str, str | None]:
        """("ok" | "other" | "gone", new cookie when this call made the claim)."""
        with self._lock:
            s = self._live()
            if s is None or not _same(s.token, token or ""):
                return "gone", None
            if s.device_hash is None:
                fresh = secrets.token_urlsafe(16)
                s.device_hash = _hash(fresh)
                s.device = device_label(user_agent)
                log.info("手機已連線：%s", s.device)
                return "ok", fresh
            if cookie and _same(_hash(cookie), s.device_hash):
                return "ok", None
            return "other", None

    def authorize(self, token: str, cookie: str | None) -> bool:
        with self._lock:
            s = self._live()
            return bool(s is not None and s.device_hash and cookie
                        and _same(s.token, token or "")
                        and _same(_hash(cookie), s.device_hash))

    def announce(self, count: int) -> None:
        with self._lock:
            s = self._live()
            if s is None:
                return
            if s.outstanding == 0:
                s.batch_total = s.batch_done = 0
            s.outstanding += max(0, count)
            s.batch_total += max(0, count)
            s.last_activity = clock()

    def begin_upload(self, name: str, total: int | None) -> None:
        with self._lock:
            self._in_flight += 1
            s = self._live()
            if s is not None:
                s.receiving = {"name": name, "bytes": 0, "total": total}
                s.last_activity = clock()

    def progress(self, nbytes: int) -> None:
        with self._lock:
            s = self._live()
            if s is not None and s.receiving is not None:
                s.receiving["bytes"] = nbytes
                s.last_activity = clock()

    def end_upload(self, ok: bool) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            s = self._live()
            if s is None:
                return
            s.receiving = None
            if s.outstanding > 0:
                s.outstanding -= 1
                s.batch_done += 1
            s.last_activity = clock()

    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def status(self) -> dict:
        with self._lock:
            s = self._live()
            if s is None:
                return {"active": False, "claimed": False, "device": "",
                        "outstanding": 0, "batch_total": 0, "batch_done": 0,
                        "receiving": None, "stalled": False, "ready": True}
            stalled = s.outstanding > 0 and clock() - s.last_activity > STALL_SEC
            return {
                "active": True,
                "claimed": s.device_hash is not None,
                "device": s.device,
                "outstanding": s.outstanding,
                "batch_total": s.batch_total,
                "batch_done": s.batch_done,
                "receiving": dict(s.receiving) if s.receiving else None,
                "stalled": stalled,
                "ready": s.outstanding == 0 or stalled,
            }


sessions = Sessions()

__all__ = ["COOKIE", "SESSION_IDLE_SEC", "STALL_SEC", "Sessions", "device_label",
           "sessions"]
