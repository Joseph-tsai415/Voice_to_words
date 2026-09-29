"""Flask app: upload -> transcribe -> correct speakers -> export."""
from __future__ import annotations

import logging
import mimetypes
import traceback
from pathlib import Path

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory)
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from . import (asr, export, gpu, hub, leaderboard, memory,
               project as store, punctuation, rework, tidy, vocabulary)
from .audio import SUPPORTED_SUFFIXES, probe_duration, upload_name_and_suffix
from .config import (ASR_MODELS, BUBBLE_GAP_SEC, CPU_COUNT,
                     DEFAULT_MODEL, DEFAULT_THREADS, MAX_CONCURRENT_JOBS,
                     PROJECTS_DIR, THREAD_CHOICES, is_downloaded,
                     language_options, missing_files, resolve_language)
from .downloads import manager
from .transcribe_queue import manager as tq
from .textproc import DEFAULT_ZH_MODE, ZH_MODE_LABELS

log = logging.getLogger("scribe.server")

def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024      # 2 GB upload cap
    app.json.ensure_ascii = False
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    # A project left as "processing" by a previous server has no worker behind it
    # any more; without this it shows a spinner that never moves.
    leaderboard.refresh_async()          # warm the ranking cache off the request path
    stale = store.recover_interrupted()
    if stale:
        log.warning("有 %d 個專案上次沒跑完，已標記為中斷：%s", len(stale), stale)

    # A dialog can't be copy-pasted, so every failure is also written to the
    # terminal in full, and the traceback is handed to the browser console.
    @app.errorhandler(Exception)
    def on_error(exc: Exception):
        if isinstance(exc, HTTPException):
            return exc
        tb = traceback.format_exc()
        log.error("未處理的錯誤 %s %s\n%s", request.method, request.path, tb)
        return jsonify(error=f"{type(exc).__name__}: {exc}",
                       where=f"{request.method} {request.path}",
                       traceback=tb), 500

    # ---------------------------------------------------------------- pages
    def _bootstrap_models() -> list[dict]:
        """Minimal list for the initial HTML; the page refreshes it over the API."""
        return hub.catalogue()

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            models=_bootstrap_models(),
            default_model=DEFAULT_MODEL,
            zh_modes=ZH_MODE_LABELS,
            default_zh_mode=DEFAULT_ZH_MODE,
            accept=",".join(SUPPORTED_SUFFIXES),
            formats=export.FORMATS,
            thread_choices=THREAD_CHOICES,
            default_threads=DEFAULT_THREADS,
            cpu_count=CPU_COUNT,
            max_jobs=MAX_CONCURRENT_JOBS,
        )

    @app.post("/api/compute/refresh")
    def api_compute_refresh() -> Response:
        """Re-probe the GPU - the card may have just been enabled."""
        gpu.refresh()
        asr.unload_recognizers()          # rebuild on the new provider
        from .config import PROVIDER
        return jsonify(gpu.status(PROVIDER))

    @app.get("/api/capacity")
    def api_capacity() -> Response:
        """What the machine is set up to do, so the UI can explain itself."""
        mem = memory.describe()
        from .config import DIARIZE_PROVIDER, PROVIDER
        compute = gpu.status(PROVIDER)
        compute["diarize_provider"] = gpu.resolve(DIARIZE_PROVIDER)[0]
        return jsonify(
            compute=compute,
            cpu_count=CPU_COUNT,
            max_jobs=MAX_CONCURRENT_JOBS,
            default_threads=DEFAULT_THREADS,
            cores_in_use=MAX_CONCURRENT_JOBS * DEFAULT_THREADS,
            running=tq.active_count(),
            memory=mem,
            loaded_models=asr.loaded_models(),
            loaded_mb=round(asr.loaded_bytes() / 1e6),
        )

    # --------------------------------------------------------------- models
    def _catalogue() -> list[dict]:
        """Model list with live download state merged in - one poll, no races."""
        active = manager.all_status()
        items = leaderboard.annotate(hub.catalogue())
        for item in items:
            state = active.get(item["key"])
            item["download"] = state
            item["busy"] = bool(state and state["state"] in ("queued", "running"))
            item["language_options"] = language_options(item["key"])
            item["language_required"] = bool(
                ASR_MODELS[item["key"]].get("language_required"))
            item["default_language"] = ASR_MODELS[item["key"]].get("default_language", "")
            item["ram_mb"] = round(asr.model_bytes(item["key"]) / 1e6)
        return items

    @app.get("/api/models")
    def api_models() -> Response:
        return jsonify({"items": _catalogue(), "leaderboard": leaderboard.meta()})

    @app.post("/api/leaderboard/refresh")
    def api_leaderboard_refresh() -> Response:
        data = leaderboard.get(force=True)
        if not data:
            return jsonify(error="無法連線到 Hugging Face 取得排行榜。"), 502
        return jsonify(leaderboard.meta())

    @app.get("/api/downloads")
    def api_downloads() -> Response:
        """Just the queue - polled while anything is downloading."""
        active = manager.all_status()
        return jsonify({
            "items": active,
            "active": sum(1 for s in active.values()
                          if s["state"] in ("queued", "running")),
        })

    @app.post("/api/models/<key>/download")
    def api_model_download(key: str) -> Response:
        if key not in ASR_MODELS:
            return jsonify(error=f"未知的模型 {key}"), 404
        try:
            record = manager.enqueue(key)          # idempotent
        except (KeyError, ValueError) as exc:
            log.warning("排入下載失敗 %s：%s", key, exc)
            return jsonify(error=str(exc)), 400
        return jsonify(record)

    @app.delete("/api/models/<key>/download")
    def api_model_download_cancel(key: str) -> Response:
        if key not in ASR_MODELS:
            return jsonify(error=f"未知的模型 {key}"), 404
        return jsonify(ok=manager.cancel(key))

    @app.delete("/api/models/<key>")
    def api_model_delete(key: str) -> Response:
        if key not in ASR_MODELS:
            return jsonify(error=f"未知的模型 {key}"), 404
        if manager.is_active(key):
            return jsonify(error=f"{ASR_MODELS[key]['label']} 正在下載中，請先取消。"), 409
        try:
            hub.delete_model(key)
            asr.unload_recognizers()
        except Exception as exc:
            log.exception("刪除模型 %s 失敗", key)
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True)

    # -------------------------------------------------------------- projects
    def _rerun_source(pid: str):
        """Audio to feed a re-run: the normalised wav, else the original upload.

        audio.wav is preferred because the upload is deleted once a run succeeds.
        """
        folder = store.project_dir(pid)
        wav = folder / "audio.wav"
        if wav.exists():
            return wav
        sources = sorted(folder.glob("source.*"))
        return sources[0] if sources else None

    def _with_progress(meta: dict) -> dict:
        """Attach the live queue record so each project carries its own status."""
        meta["job"] = tq.status(meta["id"])
        return meta

    @app.get("/api/projects")
    def api_projects() -> Response:
        items = [_with_progress(m) for m in store.list_projects()]
        return jsonify({
            "items": items,
            "active": tq.active_count(),
        })

    @app.post("/api/projects")
    def api_project_create() -> Response:
        upload = request.files.get("audio")
        if upload is None or not upload.filename:
            return jsonify(error="請選擇一個音檔。"), 400

        # Not secure_filename(): it strips non-ASCII, so a Chinese recording
        # name collapses to its own extension and the suffix comes out empty.
        # Nothing here reaches the filesystem except the suffix, which is
        # whitelisted below.
        display_name, suffix = upload_name_and_suffix(upload.filename)
        if not suffix:
            return jsonify(error="檔名沒有副檔名，看不出是什麼格式。"
                                 f"支援：{', '.join(SUPPORTED_SUFFIXES)}"), 400
        if suffix not in SUPPORTED_SUFFIXES:
            return jsonify(error=f"不支援的格式 {suffix}。支援：{', '.join(SUPPORTED_SUFFIXES)}"), 400

        model = request.form.get("model") or DEFAULT_MODEL
        if model not in ASR_MODELS:
            return jsonify(error=f"未知的模型 {model}"), 400
        if ASR_MODELS[model].get("role", "asr") != "asr":
            return jsonify(error=f"{ASR_MODELS[model]['label']} 是附加元件，"
                                 f"不能拿來辨識。"), 400
        if ASR_MODELS[model]["repo"] is None and not is_downloaded(model):
            # A bundled model can't be fetched, so this one really is unusable.
            return jsonify(error=f"{ASR_MODELS[model]['label']} 檔案不完整，缺少："
                                 f"{'、'.join(missing_files(model)[:4])}",
                           model=model, missing=missing_files(model)), 409
        if not is_downloaded(model):
            # Downloadable but not ready: the worker queues the download and
            # waits, so the upload still succeeds and shows 等待模型下載.
            log.info("模型 %s 尚未就緒，辨識工作會先等它下載完成", model)

        zh_mode = request.form.get("zh_mode") or DEFAULT_ZH_MODE
        name = (request.form.get("name") or display_name).strip() or display_name
        try:
            num_speakers = int(request.form.get("num_speakers") or -1)
        except ValueError:
            num_speakers = -1
        try:
            threads = max(1, min(CPU_COUNT, int(request.form.get("threads") or DEFAULT_THREADS)))
        except ValueError:
            threads = DEFAULT_THREADS

        options = {
            "model": model,
            "zh_mode": zh_mode,
            "num_speakers": num_speakers,
            "threads": threads,
            "language": resolve_language(model, request.form.get("language") or ""),
        }

        # Create the project first and hand the work to the queue, so the browser
        # gets an answer immediately and the project shows its own progress.
        # Many uploads can be in flight at once.
        proj = store.create(name, 0.0, model, zh_mode, options)
        source = store.project_dir(proj["id"]) / f"source{suffix}"
        upload.save(source)

        duration = probe_duration(source)      # header only, no decode
        if duration:
            store.set_duration(proj["id"], duration)

        record = tq.submit(proj["id"], str(source), options)
        log.info("新專案 %s「%s」已排入佇列（%s，%.0f 秒）",
                 proj["id"], name, model, duration)
        return jsonify(project=proj["id"], job=record)

    @app.post("/api/projects/<pid>/cancel")
    def api_project_cancel(pid: str) -> Response:
        return jsonify(ok=tq.cancel(pid))

    @app.post("/api/projects/<pid>/retry")
    def api_project_retry(pid: str) -> Response:
        try:
            proj = store.load(pid)
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404

        source = _rerun_source(pid)
        if source is None:
            return jsonify(error="原始音檔已不存在，無法重新辨識。請重新上傳。"), 400

        options = rework.options_from(proj)
        store.set_status(pid, "queued")
        record = tq.submit(pid, str(source), options)
        log.info("專案 %s 重新排入佇列", pid)
        return jsonify(project=pid, job=record)

    @app.post("/api/projects/<pid>/reprocess")
    def api_project_reprocess(pid: str) -> Response:
        """Run the whole meeting again with different settings.

        The existing transcript is discarded - that is the point of the button,
        and the UI says so before calling this.
        """
        if tq.status(pid) and tq.status(pid)["state"] in ("queued", "running"):
            return jsonify(error="這個專案正在辨識中，請先取消再重新辨識。"), 409
        source = _rerun_source(pid)
        if source is None:
            return jsonify(error="找不到這個專案的音檔，無法重新辨識。"), 400

        body = request.get_json(silent=True) or {}
        try:
            options = rework.prepare_full_rerun(pid, body)
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        record = tq.submit(pid, str(source), options)
        return jsonify(project=pid, job=record, options=options)

    @app.post("/api/projects/<pid>/segments/<seg_id>/reprocess")
    def api_segment_reprocess(pid: str, seg_id: str) -> Response:
        """Re-recognise one sentence, splitting it if several people spoke."""
        body = request.get_json(silent=True) or {}
        try:
            speakers = int(body.get("num_speakers") or -1)
        except (TypeError, ValueError):
            speakers = -1
        try:
            result = rework.rerun_segment(pid, seg_id, speakers, body)
        except (KeyError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404

        proj = result["project"]
        proj["bubbles"] = store.bubbles(proj, BUBBLE_GAP_SEC)
        proj["job"] = tq.status(pid)
        return jsonify(project=proj, summary=result["summary"])

    @app.get("/api/projects/<pid>")
    def api_project(pid: str) -> Response:
        try:
            proj = store.load(pid)
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        proj["bubbles"] = store.bubbles(proj, BUBBLE_GAP_SEC)
        proj["job"] = tq.status(pid)
        # What the text fixes would still change. A transcript is only correct
        # for the rules that existed when it ran, so an older project needs to
        # be able to say it is out of date rather than quietly staying wrong.
        proj["pending"] = tidy.pending(proj)
        return jsonify(proj)

    @app.post("/api/projects/<pid>/tidy")
    def api_project_tidy(pid: str) -> Response:
        """Re-run every text fix over a finished transcript.

        Vocabulary, then punctuation, then sentence splitting - the order they
        depend on each other in. Hand-edited sentences are left alone.
        """
        try:
            proj = store.load(pid)
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404

        counts = tidy.apply_all(proj)
        if counts["total"]:
            store.save(proj)
        proj["bubbles"] = store.bubbles(proj, BUBBLE_GAP_SEC)
        proj["job"] = tq.status(pid)
        proj["pending"] = tidy.pending(proj)
        log.info("專案 %s 整理逐字稿：%s", pid, counts)
        return jsonify(project=proj, counts=counts)

    @app.delete("/api/projects/<pid>")
    def api_project_delete(pid: str) -> Response:
        tq.cancel(pid)
        tq.forget(pid)
        store.delete(pid)
        return jsonify(ok=True)

    @app.get("/api/projects/<pid>/audio")
    def api_project_audio(pid: str) -> Response:
        folder = store.project_dir(pid)
        if not (folder / "audio.wav").exists():
            return jsonify(error="沒有音檔"), 404
        return send_from_directory(folder, "audio.wav", mimetype="audio/wav",
                                   conditional=True)

    # ----------------------------------------------------------------- edits
    def _mutate(fn, *args) -> Response:
        try:
            proj = fn(*args)
        except (KeyError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404
        proj["bubbles"] = store.bubbles(proj, BUBBLE_GAP_SEC)
        return jsonify(proj)

    @app.patch("/api/projects/<pid>")
    def api_project_patch(pid: str) -> Response:
        body = request.get_json(silent=True) or {}
        if "name" in body:
            return _mutate(store.rename_project, pid, body["name"])
        if "zh_mode" in body:
            return _mutate(store.set_zh_mode, pid, body["zh_mode"])
        return jsonify(error="沒有可更新的欄位"), 400

    @app.patch("/api/projects/<pid>/speakers/<sid>")
    def api_speaker_patch(pid: str, sid: str) -> Response:
        body = request.get_json(silent=True) or {}
        if "name" in body:
            return _mutate(store.rename_speaker, pid, sid, body["name"])
        if "color" in body:
            return _mutate(store.set_speaker_color, pid, sid, body["color"])
        return jsonify(error="沒有可更新的欄位"), 400

    @app.post("/api/projects/<pid>/speakers")
    def api_speaker_add(pid: str) -> Response:
        body = request.get_json(silent=True) or {}
        return _mutate(store.add_speaker, pid, body.get("name", ""))

    @app.delete("/api/projects/<pid>/speakers/<sid>")
    def api_speaker_delete(pid: str, sid: str) -> Response:
        target = request.args.get("reassign_to")
        return _mutate(store.delete_speaker, pid, sid, target)

    @app.post("/api/projects/<pid>/speakers/swap")
    def api_speaker_swap(pid: str) -> Response:
        body = request.get_json(silent=True) or {}
        return _mutate(store.swap_speakers, pid, body.get("a"), body.get("b"))

    @app.patch("/api/projects/<pid>/segments/<seg_id>")
    def api_segment_patch(pid: str, seg_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        if "speaker" in body:
            return _mutate(store.set_segment_speaker, pid, seg_id, body["speaker"])
        if "text" in body:
            return _mutate(store.set_segment_text, pid, seg_id, body["text"])
        return jsonify(error="沒有可更新的欄位"), 400

    @app.delete("/api/projects/<pid>/segments/<seg_id>")
    def api_segment_delete(pid: str, seg_id: str) -> Response:
        return _mutate(store.delete_segment, pid, seg_id)

    @app.post("/api/projects/<pid>/segments/<seg_id>/merge")
    def api_segment_merge(pid: str, seg_id: str) -> Response:
        return _mutate(store.merge_with_next, pid, seg_id)

    @app.post("/api/projects/<pid>/segments/<seg_id>/split")
    def api_segment_split(pid: str, seg_id: str) -> Response:
        body = request.get_json(silent=True) or {}
        return _mutate(store.split_segment, pid, seg_id, body.get("at", 1))

    @app.post("/api/projects/<pid>/segments/bulk-speaker")
    def api_segment_bulk(pid: str) -> Response:
        body = request.get_json(silent=True) or {}
        return _mutate(store.bulk_set_speaker, pid,
                       body.get("segment_ids", []), body.get("speaker"))

    # ------------------------------------------------------------ vocabulary
    @app.get("/api/vocabulary")
    def api_vocabulary() -> Response:
        return jsonify(vocabulary.load_global())

    @app.put("/api/vocabulary")
    def api_vocabulary_save() -> Response:
        body = request.get_json(silent=True)
        entries = body.get("entries") if isinstance(body, dict) else body
        return jsonify(vocabulary.save_global(entries))

    @app.post("/api/projects/<pid>/apply-vocabulary")
    def api_apply_vocabulary(pid: str) -> Response:
        """Re-apply the correction list to a transcript already produced.

        Hand-edited sentences are left alone - the user's own wording wins.
        """
        try:
            proj = store.load(pid)
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404

        rules = vocabulary.rules_for(proj)
        if not rules:
            return jsonify(error="修正表是空的，請先新增詞語。"), 400

        targets = [s for s in proj["segments"] if not s.get("edited")]
        changed, replacements = vocabulary.apply_to_segments(targets, rules)
        if changed:
            store.save(proj)
        proj["bubbles"] = store.bubbles(proj, BUBBLE_GAP_SEC)
        proj["job"] = tq.status(pid)
        log.info("專案 %s 套用詞語修正：%d 句、%d 處", pid, changed, replacements)
        return jsonify(project=proj, changed=changed, replacements=replacements,
                       skipped=len(proj["segments"]) - len(targets))

    @app.post("/api/projects/<pid>/apply-punctuation")
    def api_apply_punctuation(pid: str) -> Response:
        """Punctuate a transcript that was produced without any.

        Runs on the saved text, so a meeting recognised by a model that emits
        no punctuation becomes readable without decoding the audio again.
        Hand-edited sentences are left alone.
        """
        try:
            proj = store.load(pid)
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404

        if not punctuation.available():
            return jsonify(error="標點還原模型尚未下載，請先到模型清單下載"
                                 "「標點還原（中英）」。"), 409

        changed = 0
        for seg in proj["segments"]:
            if seg.get("edited"):
                continue
            after = punctuation.restore_if_needed(seg.get("text", ""))
            if after != seg.get("text", ""):
                seg["text"] = after
                changed += 1
        if changed:
            store.save(proj)
        proj["bubbles"] = store.bubbles(proj, BUBBLE_GAP_SEC)
        proj["job"] = tq.status(pid)
        log.info("專案 %s 套用標點還原：%d 句", pid, changed)
        return jsonify(project=proj, changed=changed)

    # ---------------------------------------------------------------- export
    @app.get("/api/projects/<pid>/export")
    def api_export(pid: str) -> Response:
        fmt = request.args.get("format", "txt")
        if fmt not in export.FORMATS:
            return jsonify(error=f"不支援的格式 {fmt}"), 400
        try:
            proj = store.load(pid)
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 404

        timestamps = request.args.get("timestamps", "1") != "0"
        merge = request.args.get("merge", "1") != "0"
        body = export.render(proj, fmt, timestamps=timestamps, merge=merge)

        _, mime, ext = export.FORMATS[fmt]
        if request.args.get("inline") == "1":
            return Response(body, mimetype=mime)
        safe = secure_filename(proj.get("name", "transcript")) or "transcript"
        resp = Response(body.encode("utf-8-sig" if fmt == "csv" else "utf-8"), mimetype=mime)
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="transcript{ext}"; '
            f"filename*=UTF-8''{_url_quote(proj.get('name', 'transcript'))}{ext}"
        )
        return resp

    return app


def _url_quote(text: str) -> str:
    from urllib.parse import quote
    return quote(text, safe="")


mimetypes.add_type("audio/wav", ".wav")
