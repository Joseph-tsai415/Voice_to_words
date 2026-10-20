"""Files a phone has sent, held until the 新增會議 dialog uses them.

The phone uploads before anyone has chosen a speaker count or a model, so
the file cannot become a project yet. It waits here, one folder per file:

    data/incoming/<id>/file<suffix>     the recording
    data/incoming/<id>/meta.json        written last - its presence means complete

The phone's filename is kept in meta.json for display only and never
reaches the filesystem, for the same reason the laptop upload is stored as
`source<suffix>`: only the whitelisted suffix does. `<id>` is checked
against a strict pattern before any path is built from it, because the
browser hands it back to the server.

A held file survives the dialog closing. Losing a recording that already
crossed the network because somebody pressed Esc would be the worst way
for this to fail.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable

from .audio import unsupported_reason, upload_name_and_suffix
from .config import DATA_DIR

log = logging.getLogger("scribe.incoming")

INCOMING_DIR = DATA_DIR / "incoming"
CHUNK = 1 << 20                      # 1 MiB: never buffer a whole recording
_ID = re.compile(r"^[0-9a-f]{12}$")


class UnsupportedFile(ValueError):
    """The suffix is not one the pipeline can read; the message says so."""


class IncompleteUpload(IOError):
    """The body ended before Content-Length - the phone locked, or the link dropped."""


def _slot(item_id: str) -> Path | None:
    return INCOMING_DIR / item_id if _ID.match(item_id or "") else None


def _remove_slot(slot: Path) -> None:
    """Best effort for a half-written slot; cleanup_partials() gets any leftover."""
    try:
        shutil.rmtree(slot)
    except OSError as exc:
        log.warning("清不掉沒傳完的暫存 %s：%s（下次啟動會再清）", slot, exc)


def receive(stream: BinaryIO, filename: str, length: int | None,
            on_progress: Callable[[int], None] | None = None) -> dict:
    """Stream one upload to disk and return its item.

    The suffix is checked before a byte of the body is read, so an
    unsupported file is refused without being transferred to disk.
    """
    filename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name, suffix = upload_name_and_suffix(filename)
    reason = unsupported_reason(suffix)
    if reason:
        raise UnsupportedFile(reason)

    item_id = uuid.uuid4().hex[:12]
    slot = INCOMING_DIR / item_id
    slot.mkdir(parents=True)
    part = slot / f"file{suffix}.part"
    written = 0
    try:
        with open(part, "wb") as out:
            while length is None or written < length:
                want = CHUNK if length is None else min(CHUNK, length - written)
                chunk = stream.read(want)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
                if on_progress:
                    on_progress(written)
        if written == 0 or (length is not None and written != length):
            raise IncompleteUpload(f"只收到 {written} / {length} bytes")
        part.replace(slot / f"file{suffix}")
        meta = {
            "id": item_id,
            "name": name,
            "filename": filename,
            "suffix": suffix,
            "bytes": written,
            "received_at": datetime.now().isoformat(timespec="seconds"),
        }
        tmp = slot / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        tmp.replace(slot / "meta.json")
    except BaseException:
        _remove_slot(slot)
        raise
    log.info("收到手機傳來的「%s」（%.1f MB）", filename, written / 1e6)
    return meta


def get(item_id: str) -> dict | None:
    slot = _slot(item_id)
    if slot is None:
        return None
    try:
        meta = json.loads((slot / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (slot / f"file{meta.get('suffix', '')}").is_file():
        return None
    return meta


def items() -> list[dict]:
    """Every completed file, oldest first."""
    if not INCOMING_DIR.is_dir():
        return []
    found = [m for m in (get(p.name) for p in INCOMING_DIR.iterdir() if p.is_dir()) if m]
    return sorted(found, key=lambda m: (m["received_at"], m["id"]))


def take(item_id: str, dest: Path) -> dict:
    """Move the file to `dest` and forget it. Same volume, so this is a rename."""
    meta = get(item_id)
    if meta is None:
        raise FileNotFoundError(f"沒有這個手機檔案：{item_id}")
    slot = INCOMING_DIR / item_id
    os.replace(slot / f"file{meta['suffix']}", dest)
    shutil.rmtree(slot)
    return meta


def discard(item_id: str) -> bool:
    slot = _slot(item_id)
    if slot is None or not slot.is_dir():
        return False
    shutil.rmtree(slot)              # raise, never pretend: see project.delete()
    return True


def cleanup_partials() -> int:
    """Remove slots with no meta.json - transfers a killed server never finished.

    Phone uploads do not resume, so a half file is worth nothing.
    """
    if not INCOMING_DIR.is_dir():
        return 0
    removed = 0
    for slot in INCOMING_DIR.iterdir():
        if slot.is_dir() and not (slot / "meta.json").exists():
            _remove_slot(slot)
            removed += 1
    return removed


__all__ = ["CHUNK", "INCOMING_DIR", "IncompleteUpload", "UnsupportedFile",
           "cleanup_partials", "discard", "get", "items", "receive", "take"]
