"""Command line entry point.

  python -m app                      啟動網頁介面
  python -m app models               列出可用模型
  python -m app pull <model>         下載模型
  python -m app run <audio> [opts]   直接在終端機產生逐字稿
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path


def _force_utf8() -> None:
    """Windows consoles default to cp950/cp1252 and would mangle the transcript."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _setup_logging(debug: bool = False) -> None:
    import logging

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="  %(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)   # quiet per-request lines


def cmd_serve(args: argparse.Namespace) -> int:
    from . import hub
    from .config import ASR_MODELS, is_downloaded, missing_files
    from .server import create_app

    _setup_logging(args.debug)
    hub.cleanup_partials()          # drop .part files from an interrupted run

    app = create_app()      # also recovers projects a previous server left running
    url = f"http://127.0.0.1:{args.port}"

    from . import gpu as gpu_mod
    from .config import PROVIDER
    device = gpu_mod.status(PROVIDER)

    ready = [k for k in ASR_MODELS if is_downloaded(k)]
    partial = [k for k in ASR_MODELS
               if not is_downloaded(k) and len(missing_files(k)) < len(ASR_MODELS[k]["files"])]
    print(f"\n  會議逐字稿工具  →  {url}\n")
    print(f"  運算裝置：{device['provider'].upper()}　{device['reason']}")
    if device["install_hint"] and device["gpu"]:
        print(f"  想用 GPU 加速： {device['install_hint']}")
    print(f"  可用模型：{', '.join(ready) if ready else '（無）'}")
    if partial:
        print(f"  下載不完整：{', '.join(partial)}  ← 到「模型設定」重新下載即可補齊")
    print("\n  錯誤訊息會完整印在這個視窗，方便複製。")
    print("  按 Ctrl+C 結束\n")

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=args.debug,
            use_reloader=args.debug, threaded=True)
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    from . import leaderboard
    from .hub import catalogue

    items = leaderboard.annotate(catalogue())
    lb = leaderboard.meta()

    print()
    for m in items:
        partial = m["files_ready"] and not m["downloaded"]
        mark = "✓" if m["downloaded"] else ("~" if partial else " ")
        print(f"  [{mark}] {m['key']:<24} {m['label']}")
        print(f"      {m['languages']} · {m['size_mb']} MB · {m.get('arch', '')}")
        print(f"      {m['note']}")

        entry = m.get("leaderboard")
        if entry:
            rank = f"總榜 #{entry['rank']}"
            if entry.get("open_rank"):
                rank += f"、開源第 {entry['open_rank']}/{entry['open_total']}"
            speed = f"、RTFx {entry['rtfx']:.0f}" if entry.get("rtfx") else ""
            print(f"      Open ASR：{rank}　WER {entry['wer']}%{speed}")
        elif m.get("upstream"):
            print("      Open ASR：未列入（此榜為英文短音檔）")

        up = m.get("hf_upstream_stats")
        if up:
            bits = [f"{up['downloads']:,} 次下載" if up.get("downloads") is not None else "",
                    f"{up['likes']} 讚" if up.get("likes") is not None else "",
                    f"授權 {up['license']}" if up.get("license") else ""]
            print(f"      上游 {up['id']}　" + "・".join(b for b in bits if b))
        if partial:
            print(f"      ⚠ 下載不完整：{m['files_ready']}/{m['files_total']} 個檔案，"
                  f"缺 {'、'.join(m['missing'])}")
        if m["repo"]:
            print(f"      🤗 {m['repo']}")
        print()
    print("  下載： python -m app pull <key>")
    print("  [✓] 完整可用   [~] 下載不完整，重新 pull 會補齊缺少的檔案\n")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    from .config import ASR_MODELS
    from .hub import download_model

    if args.model not in ASR_MODELS:
        print(f"未知的模型：{args.model}", file=sys.stderr)
        print(f"可用：{', '.join(ASR_MODELS)}", file=sys.stderr)
        return 2

    last = [""]

    def progress(stage: str, frac: float, detail: str) -> None:
        bar = "█" * int(frac * 30) + "░" * (30 - int(frac * 30))
        line = f"\r  {bar} {frac * 100:5.1f}%  {detail[:52]:<52}"
        if line != last[0]:
            sys.stdout.write(line)
            sys.stdout.flush()
            last[0] = line

    print(f"\n下載 {ASR_MODELS[args.model]['label']} …")
    download_model(args.model, progress=progress)
    print("\n  完成\n")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from . import export
    from .asr import transcribe
    from .audio import duration_of, load_16k_mono
    from .config import ASR_MODELS, is_downloaded
    from .project import bubbles, speaker_label

    if args.model not in ASR_MODELS:
        print(f"未知的模型：{args.model}", file=sys.stderr)
        return 2
    if not is_downloaded(args.model):
        print(f"{ASR_MODELS[args.model]['label']} 尚未下載。先執行："
              f" python -m app pull {args.model}", file=sys.stderr)
        return 2

    samples = load_16k_mono(args.audio)
    print(f"音檔長度 {duration_of(samples):.1f} 秒", file=sys.stderr)

    last = [""]

    def progress(stage: str, frac: float, detail: str) -> None:
        line = f"\r  {stage:<8} {frac * 100:5.1f}%  {detail[:46]:<46}"
        if line != last[0]:
            sys.stderr.write(line)
            sys.stderr.flush()
            last[0] = line

    utterances = transcribe(
        samples, args.model,
        num_speakers=args.speakers,
        zh_mode=args.zh,
        num_threads=args.threads,
        progress=progress,
    )
    sys.stderr.write("\n\n")

    names = {}
    project = {
        "name": Path(args.audio).stem,
        "duration": duration_of(samples),
        "speakers": [],
        "segments": [],
    }
    for i, cluster in enumerate(sorted({u.speaker for u in utterances})):
        sid = f"S{i + 1}"
        names[cluster] = sid
        project["speakers"].append({"id": sid, "name": speaker_label(i), "color": "#888"})
    project["segments"] = [
        {"id": f"seg-{i:04d}", "start": u.start, "end": u.end,
         "speaker": names[u.speaker], "text": u.text}
        for i, u in enumerate(utterances)
    ]

    text = export.render(project, args.format)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已寫入 {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(prog="python -m app", description="會議逐字稿工具")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="啟動網頁介面（預設）")
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7860)))
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--no-browser", action="store_true")
    p_serve.add_argument("--debug", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    sub.add_parser("models", help="列出模型").set_defaults(func=cmd_models)

    p_pull = sub.add_parser("pull", help="下載模型")
    p_pull.add_argument("model")
    p_pull.set_defaults(func=cmd_pull)

    p_run = sub.add_parser("run", help="直接產生逐字稿")
    p_run.add_argument("audio")
    p_run.add_argument("-m", "--model", default="sense-voice")
    p_run.add_argument("-s", "--speakers", type=int, default=-1, help="講者人數，-1 為自動")
    p_run.add_argument("-f", "--format", default="txt", choices=["txt", "md", "srt", "vtt", "csv", "json"])
    p_run.add_argument("-o", "--out")
    p_run.add_argument("-t", "--threads", type=int, default=4)
    p_run.add_argument("--zh", default="s2twp",
                       choices=["none", "s2twp", "s2tw", "s2hk", "t2s"])
    p_run.set_defaults(func=cmd_run)

    # "serve" is the default verb, so `python -m app --port 8000` still works.
    argv = list(sys.argv[1:] if argv is None else argv)
    verbs = set(sub.choices)
    if not argv or (argv[0] not in verbs and argv[0] not in ("-h", "--help")):
        argv.insert(0, "serve")

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
