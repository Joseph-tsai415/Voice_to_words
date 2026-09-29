"""Live rankings from the Hugging Face Open ASR Leaderboard.

Numbers shown next to a model are fetched from the leaderboard itself, not
written here. The gated results dataset (hf-audio/open-asr-leaderboard-results)
needs a token, but the Space that renders it publishes the same table in its
public Gradio config, so that is the source.

Everything is cached on disk and every failure is soft: with no network the app
falls back to the last fetch, and with no cache it simply shows no ranking.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import ASR_MODELS, DATA_DIR

log = logging.getLogger("scribe.leaderboard")

CONFIG_URL = "https://hf-audio-open-asr-leaderboard.hf.space/config"
SPACE_URL = "https://huggingface.co/spaces/hf-audio/open_asr_leaderboard"
CACHE_PATH = DATA_DIR / "leaderboard.json"

# The English short-form board is the one the Space shows first and the one
# "rank N on the Open ASR Leaderboard" usually refers to.
BOARD_HEADERS = ("Rank", "model", "Average WER ⬇️")

REFRESH_AFTER = 12 * 3600          # a day's worth of staleness is fine
TIMEOUT = 30

_guard = threading.Lock()
_memory: dict[str, Any] | None = None

_TAG = re.compile(r"<[^>]+>")


def _text(cell: Any) -> str:
    return _TAG.sub("", str(cell or "")).strip()


def _href(cell: Any) -> str:
    match = re.search(r'href="([^"]+)"', str(cell or ""))
    return match.group(1) if match else ""


def _num(cell: Any) -> float | None:
    try:
        return float(cell)
    except (TypeError, ValueError):
        return None


def _parse(config: dict) -> dict[str, Any]:
    """Pull the English short-form table out of the Space's Gradio config."""
    for comp in config.get("components", []):
        value = (comp.get("props") or {}).get("value")
        if not isinstance(value, dict):      # most components hold plain strings
            continue
        headers = value.get("headers") or []
        if not all(h in headers for h in BOARD_HEADERS):
            continue

        idx = {h: headers.index(h) for h in headers}
        rank_i = idx["Rank"]
        model_i = idx["model"]
        wer_i = idx["Average WER ⬇️"]
        rtfx_i = idx.get("RTFx ⬆️️")

        entries: dict[str, dict] = {}
        order: list[str] = []
        for row in value.get("data") or []:
            name = _text(row[model_i])
            if not name:
                continue
            entry = {
                "rank": row[rank_i],
                "name": name,
                "url": _href(row[model_i]),
                "wer": _num(row[wer_i]),
                "rtfx": _num(row[rtfx_i]) if rtfx_i is not None else None,
                "open_weights": "huggingface.co" in str(row[model_i]),
            }
            entries[name.lower()] = entry
            order.append(name)

        return {
            "fetched_at": time.time(),
            "source": SPACE_URL,
            "board": "English short-form",
            "total": len(order),
            "order": order,
            "entries": entries,
        }
    raise ValueError("Space 的設定裡找不到排行榜表格（欄位可能改了）")


def _fetch() -> dict[str, Any]:
    req = urllib.request.Request(CONFIG_URL, headers={"User-Agent": "meeting-scribe/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        config = json.load(resp)
    data = _parse(config)
    log.info("排行榜已更新：%d 個模型（來源 %s）", data["total"], SPACE_URL)
    return data


def _load_cache() -> dict[str, Any] | None:
    try:
        with CACHE_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(data: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        tmp.replace(CACHE_PATH)
    except OSError as exc:
        log.warning("排行榜快取寫入失敗：%s", exc)


def get(force: bool = False) -> dict[str, Any] | None:
    """Current leaderboard, refreshing if the cache is stale. Never raises."""
    global _memory
    with _guard:
        data = _memory or _load_cache()
        fresh = data and (time.time() - data.get("fetched_at", 0)) < REFRESH_AFTER
        if data and fresh and not force:
            _memory = data
            return data

        try:
            data = _fetch()
            _save_cache(data)
            _memory = data
        except Exception as exc:            # never let a ranking break the app
            log.warning("無法更新排行榜（%s: %s），沿用上次的資料",
                        type(exc).__name__, exc)
            _memory = data          # may be a stale cache, or None
        return _memory


def refresh_async() -> None:
    """Warm both caches in the background so startup is not blocked."""
    def work() -> None:
        get()
        try:
            stats()
        except Exception as exc:
            log.debug("模型資訊預先抓取失敗：%s", exc)

    threading.Thread(target=work, name="leaderboard", daemon=True).start()


def lookup(upstream: str | None) -> dict[str, Any] | None:
    """Leaderboard entry for a model's upstream Hugging Face id."""
    if not upstream:
        return None
    data = get()
    if not data:
        return None
    return data["entries"].get(upstream.lower())


def annotate(items: list[dict]) -> list[dict]:
    """Attach the live entry to each catalogue item, under `leaderboard`."""
    try:
        data = get()
    except Exception:
        data = None
    open_ranked = None
    if data:
        open_ranked = [n for n in data["order"]
                       if data["entries"][n.lower()]["open_weights"]]

    try:
        repo_stats = stats().get("repos", {})
    except Exception:
        repo_stats = {}

    for item in items:
        spec = ASR_MODELS.get(item["key"], {})
        upstream = spec.get("upstream")
        item["upstream"] = upstream
        item["hf_repo_stats"] = repo_stats.get(item.get("repo") or "")
        item["hf_upstream_stats"] = repo_stats.get(upstream or "")
        entry = lookup(upstream)
        if entry and open_ranked:
            entry = dict(entry)
            try:
                entry["open_rank"] = open_ranked.index(entry["name"]) + 1
                entry["open_total"] = len(open_ranked)
            except ValueError:
                pass
        item["leaderboard"] = entry
    return items


# ---------------------------------------------------------------------------
# Live repo stats from the Hub API. The leaderboard is English-only, so most of
# the Chinese models here are simply not on it; downloads/likes/last-updated are
# public for every repo and give a signal for those.
# ---------------------------------------------------------------------------
STATS_CACHE = DATA_DIR / "hf_stats.json"
STATS_REFRESH_AFTER = 12 * 3600
_stats_memory: dict[str, Any] | None = None


def _fetch_repo(repo_id: str) -> dict[str, Any] | None:
    url = f"https://huggingface.co/api/models/{repo_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "meeting-scribe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            info = json.load(resp)
    except Exception as exc:
        log.debug("讀取 %s 的資訊失敗：%s", repo_id, exc)
        return None
    return {
        "id": info.get("id", repo_id),
        "downloads": info.get("downloads"),
        "likes": info.get("likes"),
        "last_modified": info.get("lastModified"),
        "license": (info.get("cardData") or {}).get("license"),
        "url": f"https://huggingface.co/{repo_id}",
    }


def stats(force: bool = False) -> dict[str, Any]:
    """Live Hub stats for every catalogue repo, keyed by repo id."""
    global _stats_memory
    with _guard:
        data = _stats_memory
        if data is None:
            try:
                with STATS_CACHE.open(encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                data = None
        fresh = data and (time.time() - data.get("fetched_at", 0)) < STATS_REFRESH_AFTER
        if data and fresh and not force:
            _stats_memory = data
            return data

    repos = set()
    for spec in ASR_MODELS.values():
        if spec.get("repo"):
            repos.add(spec["repo"])
        if spec.get("upstream"):
            repos.add(spec["upstream"])

    fetched = {}
    for repo_id in sorted(repos):
        info = _fetch_repo(repo_id)
        if info:
            fetched[repo_id] = info

    result = {"fetched_at": time.time(), "repos": fetched}
    with _guard:
        if fetched:
            try:
                STATS_CACHE.parent.mkdir(parents=True, exist_ok=True)
                tmp = STATS_CACHE.with_suffix(".json.tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    json.dump(result, fh, ensure_ascii=False)
                tmp.replace(STATS_CACHE)
            except OSError as exc:
                log.warning("模型資訊快取寫入失敗：%s", exc)
            _stats_memory = result
        elif data:
            result = data
    log.info("已取得 %d 個 Hugging Face 儲存庫的即時資訊", len(fetched))
    return result


def meta() -> dict[str, Any]:
    """Where the numbers came from and when, for the UI to state plainly."""
    data = get()
    if not data:
        return {"available": False, "source": SPACE_URL}
    return {
        "available": True,
        "source": data["source"],
        "board": data["board"],
        "total": data["total"],
        "fetched_at": data["fetched_at"],
        "age_hours": round((time.time() - data["fetched_at"]) / 3600, 1),
    }
