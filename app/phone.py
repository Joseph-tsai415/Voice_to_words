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
STALL_SEC of no activity the button is released, and the next batch the
phone announces - the 重試 - starts the count afresh.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
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
    # This batch, one entry per file: {"name", "size", "bytes", "state"} with
    # state waiting -> sending -> done | failed. The laptop draws a row per
    # entry, and every count it shows is derived from this list, so the rows
    # and the "接收中 1/3" above them cannot disagree.
    files: list[dict] = field(default_factory=list)
    batch: int = 0                       # bumps when a fresh batch starts


@dataclass
class _Upload:
    """What begin_upload() hands back: the session and the row it fills."""
    session: _Session | None
    entry: dict | None


def _clean_files(files: list) -> list[dict]:
    """The phone's list of picked files, minus anything malformed."""
    out = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not f["name"]:
            continue
        size = f.get("size")
        ok = isinstance(size, int) and not isinstance(size, bool) and size >= 0
        out.append({"name": f["name"][:255], "size": size if ok else None})
    return out


def _outstanding(s: _Session) -> int:
    return sum(1 for f in s.files if f["state"] in ("waiting", "sending"))


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

    @staticmethod
    def _stalled(s: _Session) -> bool:
        return _outstanding(s) > 0 and clock() - s.last_activity > STALL_SEC

    def _fresh_batch_if_over(self, s: _Session) -> None:
        """Start a new batch when nothing is on its way. Lock held.

        A stalled batch counts as over: the files it was waiting for are not
        coming, so they must not hang on as "接收中 1/3" over the retry.
        """
        if _outstanding(s) == 0 or self._stalled(s):
            s.files = []
            s.batch += 1

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

    def knows(self, token: str) -> bool:
        """Is this the live session's token? Lets a refusal say *which* refusal."""
        with self._lock:
            s = self._live()
            return s is not None and _same(s.token, token or "")

    def authorize(self, token: str, cookie: str | None) -> bool:
        with self._lock:
            s = self._live()
            return bool(s is not None and s.device_hash and cookie
                        and _same(s.token, token or "")
                        and _same(_hash(cookie), s.device_hash))

    def announce(self, count: int, files: list | None = None) -> None:
        """The phone picked files: put a waiting row on the laptop for each.

        `files` carries names and sizes so the rows appear the moment the
        files are picked, before a byte has moved; without it, `count`
        nameless rows. A retry of a file that failed in a batch still in
        progress reuses its row rather than adding a second one.
        """
        with self._lock:
            s = self._live()
            if s is None:
                return
            entries = (_clean_files(files) if files is not None
                       else [{"name": "", "size": None}] * max(0, count))
            self._fresh_batch_if_over(s)
            for e in entries:
                failed = next((f for f in s.files if f["state"] == "failed"
                               and e["name"] and f["name"] == e["name"]), None)
                if failed is not None:
                    failed.update(state="waiting", bytes=0,
                                  size=e["size"] if e["size"] is not None else failed["size"])
                else:
                    s.files.append({"name": e["name"], "size": e["size"],
                                    "bytes": 0, "state": "waiting"})
            s.last_activity = clock()

    def begin_upload(self, name: str, total: int | None) -> _Upload:
        """Count an upload in; returns the session and row it belongs to.

        Pass that back to progress() and end_upload(). An upload can outlive
        its session - 換一個 QR 碼 mid-transfer - and its finish must not
        count against the new session's batch, or 開始辨識 opens while the
        new batch is still on its way. Rows are matched by name, so a
        retried file fills its own row whatever order things arrive in.
        """
        with self._lock:
            self._in_flight += 1
            s = self._live()
            if s is None:
                return _Upload(None, None)
            waiting = [f for f in s.files if f["state"] == "waiting"]
            entry = (next((f for f in waiting if f["name"] == name), None)
                     or next((f for f in waiting if not f["name"]), None))
            if entry is None:                # sent without being announced
                self._fresh_batch_if_over(s)
                entry = {"name": name, "size": total, "bytes": 0, "state": "waiting"}
                s.files.append(entry)
            entry.update(name=name, state="sending", bytes=0)
            if total is not None:
                entry["size"] = total
            s.last_activity = clock()
            return _Upload(s, entry)

    def progress(self, nbytes: int, upload: _Upload) -> None:
        with self._lock:
            s = self._live()
            if s is not None and upload.session is s:
                upload.entry["bytes"] = nbytes
                s.last_activity = clock()

    def end_upload(self, ok: bool, upload: _Upload) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            s = self._live()
            if s is None or upload.session is not s:
                return
            entry = upload.entry
            entry["state"] = "done" if ok else "failed"
            if ok:
                entry["size"] = entry["size"] if entry["size"] is not None else entry["bytes"]
                entry["bytes"] = entry["size"]
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
                        "files": [], "batch": 0, "stalled": False, "ready": True}
            stalled = self._stalled(s)
            outstanding = _outstanding(s)
            return {
                "active": True,
                "claimed": s.device_hash is not None,
                "device": s.device,
                "outstanding": outstanding,
                "batch_total": len(s.files),
                "batch_done": len(s.files) - outstanding,
                "files": [dict(f) for f in s.files],
                "batch": s.batch,
                "stalled": stalled,
                "ready": outstanding == 0 or stalled,
            }


sessions = Sessions()


# ------------------------------------------------------------------ listener
# The main app stays on 127.0.0.1. The phone reaches a second, separate
# server whose Flask app has exactly the three routes below, so there is no
# route guard to get wrong: transcripts, audio, edit and delete do not exist
# on this port at all. It is started when the 用手機傳 tab opens and stopped
# as soon as there is no session and nothing in flight, so nothing listens
# on the network while the feature is not in use.

REAP_SEC = 5.0
# A phone that walks out of Wi-Fi range, or that iOS suspends, can vanish
# without closing its connection. With no timeout its upload would block in
# recv() forever, keep in_flight above zero, and so keep this port open on
# every interface until the server restarts. Applied per recv(), so a slow
# but live transfer is unaffected.
SOCKET_TIMEOUT_SEC = 120.0

GONE = "此 QR 碼已失效，請在電腦上重新產生。"
OTHER = "此 QR 碼已在另一台裝置使用。請在電腦上重新產生。"
INCOMPLETE = "沒有傳完，請重試。"

_srv_lock = threading.Lock()
_server = None                   # werkzeug BaseWSGIServer while listening


def create_phone_app():
    from flask import Flask, jsonify, make_response, render_template, request
    from urllib.parse import unquote
    from werkzeug.exceptions import ClientDisconnected

    from . import incoming
    from .audio import SUPPORTED_SUFFIXES, unsupported_reason

    app = Flask(__name__, static_folder=None, template_folder="templates")
    app.config["MAX_CONTENT_LENGTH"] = None        # a phone video can pass 2 GB
    app.json.ensure_ascii = False

    def deny(token: str):
        return (jsonify(error=OTHER), 403) if sessions.knows(token) \
            else (jsonify(error=GONE), 404)

    @app.errorhandler(Exception)
    def on_error(exc: Exception):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        log.exception("手機上傳出錯 %s %s", request.method, request.path)
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 500

    @app.get("/u/<token>")
    def page(token: str):
        verdict, fresh = sessions.claim(token, request.cookies.get(COOKIE),
                                        request.headers.get("User-Agent", ""))
        if verdict == "gone":
            return render_template("phone.html", message=GONE), 404
        if verdict == "other":
            return render_template("phone.html", message=OTHER), 403
        resp = make_response(render_template(
            "phone.html",
            token=token,
            accept=",".join(SUPPORTED_SUFFIXES),
            suffixes=SUPPORTED_SUFFIXES,
            # One wording for both ends: the page fills the suffix into this.
            no_suffix=unsupported_reason(""),
            bad_suffix=unsupported_reason(".__SUFFIX__"),
        ))
        resp.headers["Cache-Control"] = "no-store"
        if fresh:
            # Lax, not Strict: re-scanning the code is a navigation from the
            # camera app, and Strict would withhold the cookie and refuse the
            # very phone that owns the session.
            resp.set_cookie(COOKIE, fresh, path=f"/u/{token}",
                            httponly=True, samesite="Lax")
        return resp

    @app.post("/u/<token>/batch")
    def batch(token: str):
        if not sessions.authorize(token, request.cookies.get(COOKIE)):
            return deny(token)
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        files = data.get("files")
        if isinstance(files, list):
            sessions.announce(len(files), files)
        else:
            try:
                sessions.announce(int(data.get("count", 0)))
            except (TypeError, ValueError):
                sessions.announce(0)
        return jsonify(ok=True)

    @app.put("/u/<token>/file")
    def upload(token: str):
        if not sessions.authorize(token, request.cookies.get(COOKIE)):
            return deny(token)
        # Headers are latin-1, so the page percent-encodes the name.
        name = unquote(request.headers.get("X-Filename", ""))
        owner = sessions.begin_upload(name, request.content_length)
        ok = False
        try:
            item = incoming.receive(request.stream, name, request.content_length,
                                    lambda n: sessions.progress(n, owner))
            ok = True
            return jsonify(ok=True, item=item)
        except incoming.UnsupportedFile as exc:
            return jsonify(error=str(exc)), 400
        except (incoming.IncompleteUpload, ClientDisconnected) as exc:
            log.warning("「%s」沒有傳完：%s", name, exc)
            return jsonify(error=INCOMPLETE), 400
        finally:
            sessions.end_upload(ok, owner)

    return app


def ensure_listener(host: str = "0.0.0.0") -> int:
    """Start the phone listener if it is not running; return its port."""
    from werkzeug.serving import WSGIRequestHandler, make_server

    class _PhoneHandler(WSGIRequestHandler):
        # The timeout surfaces as TimeoutError, which Werkzeug's LimitedStream
        # turns into ClientDisconnected - upload() already answers that with
        # a 400 and releases the upload.
        timeout = SOCKET_TIMEOUT_SEC

    global _server
    with _srv_lock:
        if _server is None:
            srv = make_server(host, 0, create_phone_app(), threaded=True,
                              request_handler=_PhoneHandler)
            threading.Thread(target=srv.serve_forever, name="phone-listener",
                             daemon=True).start()
            threading.Thread(target=_reap, args=(srv,), name="phone-reaper",
                             daemon=True).start()
            _server = srv
            log.info("手機上傳埠已開啟 %s:%d", host, srv.server_port)
        return _server.server_port


def _reap(srv) -> None:
    """Close the port once the dialog is gone, even if it never said goodbye."""
    while True:
        time.sleep(REAP_SEC)
        with _srv_lock:
            if _server is not srv:
                return
        sessions.status()                    # expires an abandoned session
        stop_listener_if_idle()


def stop_listener_if_idle() -> bool:
    """Stop listening when there is no session and no upload in flight."""
    global _server
    with _srv_lock:
        srv = _server
        if srv is None or sessions.status()["active"] or sessions.in_flight():
            return False
        _server = None
    srv.shutdown()
    srv.server_close()
    log.info("手機上傳埠已關閉")
    return True


def listener_port() -> int | None:
    with _srv_lock:
        return _server.server_port if _server is not None else None


def phone_url(host: str, port: int, token: str) -> str:
    return f"http://{host}:{port}/u/{token}"


def qr_svg(url: str) -> str:
    import segno
    return segno.make_qr(url, error="m").svg_inline(scale=6, border=2)


__all__ = ["COOKIE", "REAP_SEC", "SESSION_IDLE_SEC", "STALL_SEC", "Sessions",
           "create_phone_app", "device_label", "ensure_listener", "listener_port",
           "phone_url", "qr_svg", "sessions", "stop_listener_if_idle"]
