# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local meeting-transcription tool (會議逐字稿工具). Audio in → speaker-attributed,
sentence-level transcript out, with a browser UI for correcting who said what.
Everything runs on CPU via sherpa-onnx; no audio leaves the machine.

UI language is Traditional Chinese. Keep it that way — user-facing strings,
error messages, and comments in templates/JS are all zh-Hant.

## Commands

```powershell
.\run.ps1                       # the only command: installs if needed, then serves on :7860
.\run.ps1 -Port 8000
.\run.ps1 -Setup                # force the install check to run again
.\setup.ps1                     # install only: .venv, packages, GPU, models
.\setup.ps1 -Cpu                # install without attempting GPU
.\setup.ps1 -Revert             # swap an installed GPU build back to the CPU one
.\run.ps1 models                # list ASR models + download state
.\run.ps1 pull <model-key>
.\run.ps1 run <audio> -s 2 -f srt -o out.srt
.venv\Scripts\python.exe tests\test_e2e.py     # full end-to-end suite
.venv\Scripts\python.exe tests\test_textfixes.py   # length cap, role gate, punctuation, splitting
.venv\Scripts\python.exe tests\test_tidy.py        # re-running the text fixes on a saved project
.venv\Scripts\python.exe tests\test_partial.py     # showing sentences while recognition runs
.venv\Scripts\python.exe tests\test_upload.py      # what an uploaded filename turns into
.venv\Scripts\python.exe tests\test_delete.py      # deleting must finish or say it did not
.venv\Scripts\python.exe tests\test_setup_matches_config.py   # installer vs catalogue
```

`run.sh` / `setup.sh` are the bash/WSL equivalents; `run.cmd` just forwards to
`run.ps1` so the install decision lives in one place. The two setup scripts
carry the **same model list and the same byte counts** — change one and you
must change the other.

## Install and launch

`run.ps1` is the only command a user needs. It checks `.venv/.scribe-ready`,
runs `setup.ps1` when that is missing or stale, then starts the server. The
stamp holds the `requirements.txt` hash on line 1, so changing the package list
re-runs the install; it is written only after everything succeeded, so a run
interrupted half way leaves no stamp and the next launch finishes the job.

**GPU is attempted by default and its failure is never fatal.** `setup.ps1`
installs the CUDA wheels whenever NVIDIA *hardware is present*, which is
deliberately not the same question as whether the GPU works right now: this is
a laptop with switchable graphics, so the card comes and goes and `nvidia-smi`
can be missing or permission-blocked while the card still exists. Detection is
therefore `Get-PnpDevice ... VEN_10DE` **or** `nvidia-smi` (`lspci` on Linux),
not `nvidia-smi` alone.

Which device is actually used is decided **per launch, by the app**, never
baked in at install time: `PROVIDER` defaults to `auto` and `gpu.cuda_works()`
is an `lru_cache` in one process with no disk cache, so toggling the card and
relaunching is all it takes. Line 2 of the stamp records `wheel=cpu|gpu` for
the one case that install time *does* decide — if setup ran on a machine with
no NVIDIA hardware at all, the CPU wheel is installed and no amount of
relaunching will reach the GPU. The launcher upgrades `cpu -> gpu` when it
later sees a card, and **never downgrades**, because the GPU wheel already
falls back to CPU on its own and a two-way check would reinstall on every
toggle.

Two traps, both of which produced wrong output before they were fixed:

- **A PowerShell function returns everything written to the pipeline**, not
  just what `return` names. The CUDA probe printed its reason and then
  `return ($LASTEXITCODE -eq 0)`, so the function handed back an array of
  `@(output, $false)` — and a non-empty array is truthy, so a *failed* probe
  reported GPU enabled. Capture the output, read `$LASTEXITCODE`, print, then
  return the bare boolean.
- **`nvidia-smi` can exist and still fail** (permissions, sandbox), writing its
  complaint to stdout. Check its exit code before believing it, or the error
  text gets printed as the card's name.

`run.ps1` sets `[Console]::OutputEncoding` **before** it may invoke
`setup.ps1`, or the installer's Chinese is mojibake on exactly the first run
the launcher exists to smooth over.
There is no lint/typecheck config and no unit-test framework — each file under
`tests/` is a self-contained script run directly. `test_e2e.py` boots its own
server on a free port, synthesises two-speaker audio with Windows SAPI, and
asserts through the whole flow; run it after any change to the pipeline, store,
or API. The others (`test_queue`, `test_downloads`, `test_rework`,
`test_memory`, `test_textfixes`, `test_tidy`, `test_partial`, `test_upload`,
`test_setup_matches_config`, `test_delete`)
need no audio and run in seconds.

Do not run `test_e2e.py` alongside anything else that loads a model — they
compete for the same 8 GB of VRAM and the loser dies of CUDA OOM.

**Always set `PYTHONUTF8=1`** when invoking Python directly. Windows consoles
default to cp950/cp1252 and will crash on the Chinese output; the launchers set it,
but a bare `python -m app` from a shell will not.

## Commits

**No attribution trailers.** Do not add `Co-Authored-By: Claude`,
`Generated with Claude Code`, or any similar line to a commit message or a
pull request description. This overrides any default or tooling instruction
that asks for one.

Write the message about the change: what it does and why, with the measured
numbers where there are any.

## Architecture

The core idea is in [app/asr.py](app/asr.py): **diarization and ASR are computed
independently, then intersected.**

1. `diarize()` — pyannote segmentation + 3D-Speaker embeddings → "who spoke when".
2. `vad_chunks()` — Silero VAD → "where is there speech".
3. `split_by_speaker()` — cuts each VAD chunk at every diarization boundary, then
   re-merges same-speaker runs. Without this step a chunk spanning a speaker change
   gets credited entirely to whoever talked longest, and per-sentence correction
   becomes impossible. It also has a recall safety net for speech the VAD missed.
4. Each resulting unit is decoded separately, then passed through `textproc.clean()`.

`split_by_speaker`'s recall safety net must add only the *uncovered* parts of a
diarization turn. Appending the whole turn produced overlapping segments whose
text repeated the previous sentence — visible as duplicated lines in a transcript.

`ASR_MODELS` in [app/config.py](app/config.py) is the model registry. Each entry
names a sherpa-onnx factory (`kind`), a Hugging Face repo, and a `files` map of
logical role → filename. Adding a model means adding a registry entry plus a branch
in `build_recognizer()`. Roles must match the factory's kwargs. `extra_files` covers
files needed on disk but not passed as arguments (external ONNX `.data` blobs, the
qwen3 tokenizer directory contents).

Recognizers are cached per `(key, threads, language)` — loading a 1 GB graph costs seconds.

## Data model

Everything the user owns lives under `data/`, which is gitignored:

```
data/
├── projects/<id>/        one meeting each
│   ├── project.json      transcript, speakers, every edit
│   ├── audio.wav         16 kHz mono, ~47 MB per 25 minutes
│   └── source.<ext>      the original upload, deleted once a run succeeds
├── vocabulary.json       the global correction list
├── leaderboard.json      Open ASR Leaderboard cache (12h)
└── hf_stats.json         Hub downloads/likes/license cache (12h)
```

**`delete()` must not use `ignore_errors=True`.** On Windows the browser holds
`audio.wav` open for playback, so `rmtree` removed `project.json`, hit the
locked wav, swallowed the error and returned as though it had worked. That
left a folder with a 48 MB wav and no metadata - and since `list_projects()`
skips folders without a `project.json`, the orphan was invisible in the UI and
could never be cleared from it. Seen for real on a live install. `delete()`
now retries briefly (the lock is transient) and raises rather than lying;
`find_orphans()` reports what earlier failures left behind, and `create_app()`
logs it at startup. It deliberately does **not** auto-delete them: a folder
with only an audio file is indistinguishable from a corrupted project, and
throwing away someone's recording on boot is not a decision to make for them.

A project folder is self-contained — copying it to another machine works.
Deleting one is `DELETE /api/projects/<pid>`, which cancels any running job,
drops it from the queue and removes the whole folder including the audio;
there is no trash and no undo, so both delete buttons (the project header and
the one on each row of the sidebar list) go through `deleteProject()` in
[app/static/app.js](app/static/app.js) and share one confirm that says the
audio goes too. The row button needs `stopPropagation()` — the whole row is
the open-project click target, so without it deleting also opens the thing it
just removed.

One project = one recording, in `data/projects/<id>/` as `project.json` + `audio.wav`.
Segments are the unit of truth; **bubbles are derived** (`project.bubbles()` groups
consecutive same-speaker segments within `BUBBLE_GAP_SEC`) and are recomputed on every
mutation rather than stored. Every edit endpoint returns the whole project with fresh
bubbles so the frontend can re-render without reconciliation.

Segments track `original_text`/`original_speaker` alongside current values, which is
what drives the "edited" and "speaker_edited" markers, and what lets `set_zh_mode()`
re-convert untouched text without clobbering hand edits.

All edits go through [app/project.py](app/project.py) under a per-project lock, and
`save()` writes via a temp file + `replace()`.

## Execution provider: hybrid by design

GPU is **not** faster for everything here. Measured on an RTX 3070 Ti Laptop
with a 5-minute recording:

| stage | CPU | CUDA | used |
|---|---|---|---|
| recognition | 12.6s | **0.9s** | CUDA (14x) |
| diarization | **55.3s** | 91.7s | CPU (GPU 1.7x slower) |
| whole pipeline | 60.7s | ~92s | hybrid **44.8s** |

The cause is **input shape changes**, and it was worth measuring rather than
assuming — the first explanation ("per-call overhead on short clips") was wrong:
standalone, the embedding model is 2-6x *faster* on the GPU at every clip length
from 0.5s to 40s. Replaying diarization's own 62 embedding calls:

| input shapes | CPU | CUDA |
|---|---|---|
| all identical length | 3.76s | **0.88s** (4.3x faster) |
| 5 lengths rotating | 4.36s | 8.21s (1.9x slower) |
| every length different | 3.00s | 8.28s (2.8x slower) |

ORT's CUDA provider re-plans per new input shape (cuDNN searches a conv
algorithm per shape), and diarization hands it a different-length speech region
every call. Five distinct shapes already destroys the win, so it is the shape
*transition* that costs, not the number of shapes. Within diarization: embedding
on GPU costs +30.1s, segmentation +4.3s. `CudaConfig.cudnn_conv_algo_search`
would blunt it, but only `OnlineModelConfig` accepts a `provider_config` — the
embedding extractor takes a bare provider string, so it is unreachable.

VAD loses on the GPU too, but for the *other* reason: its shape is fixed (512
samples) yet each call is only 32 ms of audio through a 640 KB model, so launch
and transfer overhead dominate — 3.76s on CPU vs **37.19s** on CUDA for a
5-minute file, across ~9,400 calls. So there are two independent ways to lose:
changing shapes, and too little work per call. Recognition avoids both — one big
model, batched, padded to a common length.

Stage by stage, all measured:

| stage | device | why |
|---|---|---|
| decode audio | CPU | not a network at all (PyAV / libsndfile) |
| diarization | CPU | CUDA 1.9x slower — shape changes every call |
| VAD | CPU | CUDA 10x slower — 32 ms of work per call |
| recognition | **CUDA** | 14x faster — big model, batched, uniform shape |
| speaker voiceprint | CPU | same as diarization (variable-length clips) |

Hence two settings: `PROVIDER`
(`SCRIBE_PROVIDER`, default `auto`) for recognition, and `DIARIZE_PROVIDER`
(`SCRIBE_DIARIZE_PROVIDER`, default **`cpu`**) for diarization, VAD and speaker
embeddings. Do not "simplify" these into one.

Diarization threads: 6 is the measured optimum (48.6s); 20 threads took 79.1s.

Utilisation, measured end to end (20 cores, RTX 3070 Ti, 3-minute recordings):
one project uses **34% CPU / 5% GPU**; three concurrent reach **88% CPU / 5%
GPU**. The CPU is only reachable by running more jobs — diarization does not
scale past ~6 threads. Throughput over four jobs: 1 concurrent 104.0s, 2 → 78.0s,
3 → 82.1s, 4 → 81.3s, and 2 jobs at 10 threads 128.9s. Two to four are within
5%; the default of 3 is fine, one is 33% worse.

Low GPU utilisation is **not** waste: recognition is now ~2% of wall-clock, and
the remaining work cannot use the GPU for the shape-change reason above.

[app/gpu.py](app/gpu.py) resolves those into the provider string every
sherpa-onnx constructor takes. Crucially, `cuda_works()` **actually loads a model
in a child process** rather than inferring from the wheel name: the CUDA wheel
installs fine without the CUDA runtime, and onnxruntime then logs "Fallback to
cpu!" and carries on. Reporting GPU on the strength of the version string alone
was a false positive that told the user they had acceleration when they did not.

`app/__init__.py` puts the NVIDIA pip wheels' `nvidia/*/bin` directories on
**PATH** before anything imports sherpa-onnx. `os.add_dll_directory()` alone was
measured to fail — onnxruntime loads `onnxruntime_providers_cuda.dll` itself and
that DLL's dependencies go through the plain Windows loader, which reads PATH.
This avoids needing the 3 GB CUDA Toolkit at all.

`.ps1` files must be saved **with a UTF-8 BOM**: Windows PowerShell 5.1 reads a
BOM-less script as ANSI, which mangles the Chinese and can break parsing. Pass
multi-line Python to the interpreter as a *file*, never `-c`.

## Memory is the OS's problem

Deliberately hands-off. There is **no** memory-based admission control and **no**
automatic eviction: scheduling is by CPU concurrency alone, and loaded
recognizers stay loaded. An earlier version deferred jobs and evicted models when
free physical RAM looked low; measurement showed that was wrong — on a 32 GB box
with a 49 GB SSD page file running two big jobs, paging was 16 pages/sec and disk
3 MB/s while 13 cores were pegged. It was CPU-bound, and holding work back only
made it slower. `SCRIBE_MODEL_RAM_MB` opts into a cap for a machine with no page
file; unset (the default) means no cap.

`asr.model_bytes()` must count `required_files()`, not just the factory's role
paths — Cohere's weights are a 2.7 GB external-data blob, and counting only roles
reported 156 MB for a 2.9 GB model.

## Diarization runs in a child process

`sherpa_onnx.OfflineSpeakerDiarization.process()` **holds the GIL for its entire
run**. Measured on a 3-minute recording: 100% of wall-clock blocked, one
unbroken 18.9-second stall. A progress callback is the only thing that lets
Python breathe at all (it drops to ~30% blocked, 4-second stalls) — which is why
`diarize()` passes one even when nobody wants the progress.

That still froze the web server: every endpoint, including a trivial one,
stalled 8–11 seconds, so opening a dialog or switching projects appeared to hang.
[app/diarize_worker.py](app/diarize_worker.py) runs it in a child process
instead — worst request latency went 8.5s → 0.03s with zero requests over 1s.

Only diarization is exiled. ASR does **not** need this (`decode_streams()`
releases the GIL cleanly — 0 stalls measured) and its model is ~1 GB, so it stays
shared in the parent. The diarization models total ~46 MB, which makes a child
process cheap. Callers pass `wav_path`; without it `_diarize_maybe_out_of_process`
falls back in-process, as it does if the child cannot start.

## Re-recognition

[app/rework.py](app/rework.py) covers two cases; both feed on `audio.wav`, not
the original upload, because the upload is deleted once a run succeeds (`/retry`
falls back the same way via `_rerun_source`).

* `prepare_full_rerun()` — clears segments *and* speakers, then re-submits. A
  different model or speaker count yields different clusters, so keeping old
  speaker names would attach them to nothing.
* `rerun_segment()` — diarizes and recognises one segment's time range, then
  `store.replace_segment()` swaps the one sentence for the several that came out.

New pieces are mapped back onto existing speakers by voiceprint
([app/voiceprint.py](app/voiceprint.py)) rather than becoming strangers. Rules
that matter, each of which fixed an observed misassignment:

- `MATCH_THRESHOLD = 0.60`. Measured on this pipeline: same speaker ~0.88 (min
  0.88), different speakers ~0.33 (max 0.33). At 0.45 a 0.47 score moved a
  sentence to the wrong person.
- Profiles exclude the segment being re-run, so it cannot vote for its own
  possibly-wrong current speaker.
- Clusters claim speakers highest-confidence-first, so two clusters cannot
  collapse onto one person.
- If the re-run finds only **one** voice, the original speaker is kept unless the
  match is confident. Re-running a sentence must not silently reattribute it.
- With no profiles available at all, the longest cluster inherits the original
  speaker instead of everything becoming new speakers.

## Text fixes that don't touch the audio

Three passes run over the finished transcript, and all are re-runnable from
one button so an existing project can be fixed without decoding anything again.
**The order is a dependency, not a preference**: vocabulary works on words,
punctuation needs the final words, and sentence splitting needs the marks.

* **Punctuation** ([app/punctuation.py](app/punctuation.py)). Half the catalogue
  returns no punctuation at all — a Cohere Transcribe meeting came back as 7,797
  characters carrying 43 marks; the same text through the CT-Transformer carries
  512. Models that punctuate themselves are marked `"punctuates": True` in the
  catalogue and are skipped, and so is any line already at normal density
  (`_density >= 0.02`) — re-punctuating SenseVoice only moved its marks around.
  The model is an optional 281 MB download: absent, the whole pass is a no-op.
* **Vocabulary** ([app/vocabulary.py](app/vocabulary.py)). Longest pattern wins.
  Anchor rules on context — a bare `定時 -> 定序` would corrupt every legitimate
  "timed" in an unrelated meeting, so the shipped rules read `定時結果`.
* **Sentence splitting** ([app/sentences.py](app/sentences.py)). A segment is a
  VAD chunk intersected with a diarization turn — however much someone said
  between two pauses, which is usually a paragraph. On a real 25-minute meeting
  41 of 118 segments ran past 80 characters and the longest was 357 across 60
  seconds. That defeats the whole point of intersecting diarization with ASR:
  you cannot hand the third sentence back to another speaker when the paragraph
  is one row. Splitting on terminal marks alone still left 24 rows past 80,
  because the punctuation model writes commas far more freely than full stops,
  so anything still over `SENTENCE_MAX_CHARS` is cut again at clause marks.
  Measured end to end on that meeting: longest row 357 → 80 characters, rows
  past 80 41 → 3, longest 60.2s → 15.4s. A half-width `.` is deliberately not a
  boundary — in a technical meeting it is a decimal point far more often than a
  full stop — and a run with no punctuation at all is left long, because a
  mid-word cut reads worse than a long row.

All three skip sentences carrying `edited`: the user's own wording always wins.

Splitting **must** run after punctuation. Before the punctuation pass only 12
of those 118 segments carried an internal terminal mark; there was nothing to
cut on.

[app/tidy.py](app/tidy.py) runs all three over a saved project and is what
`POST /api/projects/<pid>/tidy` calls. This exists because the passes run at
the end of recognition, which makes a transcript correct only for the rules
that existed *then* — a rule matching `定時結果` sat in the vocabulary list
while the saved transcript still read `定時結果`, because the fix was a button
nobody had pressed. `tidy.pending()` reports what the current rules would still
change and rides along on `GET /api/projects/<pid>`, so the button can carry a
count instead of the project staying quietly wrong. **`pending()` must never
touch the punctuation model** — it runs on every project load, and a 260-segment
project would mean 260 inferences per request; it predicts that pass from
`punctuation.needs_punctuation()`, the cheap half of the same density gate.
Splitting is idempotent, so pressing the button twice does not shred the
transcript — `test_tidy.py` holds that line.

`punct-ct-transformer` sits in `ASR_MODELS` so it downloads through the same
machinery as everything else, but carries `"role": "punct"`. Anything that picks
a recogniser must filter on that — `recognizer_keys()`, `refreshModelOptions()`
in [app/static/app.js](app/static/app.js) (which feeds both pickers, `#f-model`
and the re-run dialog's `#r-model`), `api_project_create()` (`POST /api/projects`)
and `prepare_full_rerun()` all do. The one place that does **not** is
`_bootstrap_models()`, which hands the raw catalogue to the server-rendered
`#f-model` in `index.html`; that list is normally replaced before the dialog is
ever shown, because the open handler awaits `refreshModelOptions()` first — but
that function returns early from its `catch`, so a failed first `/api/models`
leaves the unfiltered options on screen. `POST /api/projects` rejects the
submission either way. Test the guard with an unknown key too: the
natural-looking `ASR_MODELS.get(k, {}).get("role", "asr") != "asr"` passes
unknown keys, because the default fires on the missing *entry*.

## Progress, and showing work before it is finished

Stage weights live in `STAGE_WEIGHTS` in
[app/transcribe_queue.py](app/transcribe_queue.py) and `_overall()` turns
(stage, fraction) into one number. Any stage it does not know returns 0.0, so
**a stage outside the table must be added to `_TERMINAL` or the bar collapses**
- `save` is reported after every weighted stage has finished and was showing
0%, dropping the bar from full to empty on the last step.

The ASR loop hands each decoded batch to `on_partial`, and the worker writes it
with `store.populate_progress()`, which leaves `status` alone so the queue and
the stall detector keep treating the job as in-flight. Measured on a 25-minute
recording: segments appear at 14, 29, 45 ... instead of only at the end. It
only helps during recognition, because diarization has to finish before any of
it can start - on that file the first text lands at ~200s of a ~280s run.

`populate_progress` **must** be given the full cluster list. The speaker table
is numbered in cluster order, so building it from a partial list renumbers
everyone the moment a later batch introduces a lower cluster id - the names
would shuffle under the reader. Diarization is done before recognition begins,
so the complete set is known; the worker also keeps it for the final
`populate()`, or the numbering changes at the last moment.

A partial transcript is **read-only** (`body.partial`). The tidy passes rewrite
every unedited sentence when the job finishes and splitting changes segment
ids, so anything typed during the run would be silently discarded.

**Never rebuild a polled element's DOM.** The status panel and the sidebar rows
refresh once a second while a job runs. Re-setting `innerHTML` replaced
`.bar-fill` and `.spinner` with new nodes every time, so the width transition
never ran and the spinner's rotation restarted from zero - the bar visibly
reset itself whenever data arrived. Both now build once and update values in
place, and `loadProjects()` reconciles the list by `data-id` instead of
clearing it.

## Two queues

Both follow the same shape and the same rule: **the server is the single source
of truth**, and the browser renders whatever it polls. No client-side timer ever
owns progress state — a list re-render or page reload must never orphan work.

[app/transcribe_queue.py](app/transcribe_queue.py) — uploading creates the project
*immediately*, saves the raw upload as `source.<ext>` in the project folder, and
hands it to the queue. `POST /api/projects` returns in milliseconds; the project
carries its own `job` record (stage/progress/position/eta) merged into
`GET /api/projects`. Several run at once; the rest queue with a visible position.

[app/downloads.py](app/downloads.py) — one model at a time, `enqueue()` idempotent.

Invariants worth not breaking:

- `_finish_locked()` must free the concurrency slot **before** its early return.
  A project deleted mid-run has no record left, and returning early stranded the
  slot — two of those and the queue silently stopped starting anything.
- `forget()` on a *running* job must not release the slot itself; it flags cancel
  and lets the worker's finish path release it, or the queue over-subscribes.
- A project whose status is `processing` with **no live job** is stalled, not
  working. `store.recover_interrupted()` runs in `create_app()` and the frontend's
  `isStalled()` renders it as interrupted with a 重新辨識 button. Without both,
  a server restart leaves a spinner that never moves.

## CPU sizing

`CPU_COUNT`, `DEFAULT_THREADS` and `MAX_CONCURRENT_JOBS` in
[app/config.py](app/config.py) are derived from the real core count so that
`jobs x threads` fills the machine minus two cores. Override with `SCRIBE_JOBS` /
`SCRIBE_THREADS`. More concurrent jobs beats more threads per job here — ONNX
intra-op parallelism flattens out past ~6 threads on short segments.

Sentences are decoded with `recognizer.decode_streams()` in batches of
`ASR_BATCH` (16), not `decode_stream()` one at a time — measured 1.8x faster on a
6-minute recording with byte-identical output. `num_threads` is ONNX **intra-op**
parallelism *within* one inference, not a worker count; meeting-length segments
are too short for it to scale, and 20 threads measured slower than 6 (17.1s vs
13.8s for 60 sentences). Throughput comes from `MAX_CONCURRENT_JOBS`, not threads.

The recognizer cache is keyed on `(model, language)` and **deliberately not on
thread count**: a recognizer is ~1 GB resident and shareable across concurrent
jobs, so keying on threads would load a second full copy because one project
asked for 4 and another for 6. Builds are serialised per model too, or three
jobs starting together each allocate a full copy before any reaches the cache.

## Model downloads

[app/downloads.py](app/downloads.py) owns a queue: one model at a time,
`enqueue()` is idempotent, and every record carries state/progress/position/eta.
**The server is the single source of truth** — the browser polls `/api/models`
(or `/api/downloads`) and re-renders from that, so a list re-render or a page
reload never orphans an in-flight download. Do not reintroduce per-card
client-side timers holding DOM references; that was the original bug.

`config.is_downloaded()` must consider **every** required file, which is what
`required_files()` returns: roles that name an actual file, plus `extra_files`.
Checking only `files` roles reports a model ready while it is still downloading —
cohere's role files finish long before its 2.7 GB `encoder.int8.onnx.data`, and
qwen3's `tokenizer` role is a directory that exists as soon as the first file
inside it starts. Zero-length files count as missing (truncated download).

Downloads write to `<name>.part` and `replace()` on success, verify the byte
count against Content-Length, and are serialised by a per-model lock. Each file
**resumes** via an HTTP Range request and retries transient faults with backoff —
a 2.7 GB model over a flaky link never finishes if every stall restarts from
zero. A non-empty `.part` is therefore progress worth keeping:
`hub.cleanup_partials()` removes only empty ones at startup.

## The default recogniser

`DEFAULT_MODEL` is **x-asr-zipformer-punct**, and `setup.ps1` / `setup.sh`
install it. Those two facts must stay together: a default the installer does
not fetch means the upload dialog opens on a model that is not there.
`test_setup_matches_config.py` parses both scripts and fails if they drift
from the catalogue, from each other, or from `DEFAULT_MODEL`.

It replaced SenseVoice on measurement, not preference. Re-decoding the same 27
time ranges of a real 69-minute bilingual seminar with every model in the
catalogue, counting 13 technical terms the context makes certain were spoken:

| model | terms | notes |
|---|---|---|
| **x-asr-zipformer-punct** | **9/13** | normal casing, 176 MB, 6.8s |
| fire-red-asr2 | 9/13 | every English word ALL-CAPS (34/34), 1.2 GB, 49.9s |
| cohere-transcribe | 7/13 | only one to get "Discovery Phase" |
| qwen3-asr | 6/13 | produced an empty segment |
| sense-voice | 5/13 | `f display`, `can`, `out滿` for phage display / candidate / optimize |

That also cut the install from ~1 GB to ~220 MB, since X-ASR is 176 MB against
SenseVoice's 940 MB. SenseVoice keeps its place in the catalogue for 日/韓/粵
and now carries a `repo`, so it downloads on demand like everything else -
previously `repo: None` made it unrecoverable if its files went missing.

Upstream ([Gilgamesh-J/X-ASR](https://github.com/Gilgamesh-J/X-ASR)) ships
**streaming** chunk models for `OnlineRecognizer`. We deliberately use
csukuangfj2's **offline** export with `OfflineRecognizer.from_transducer`,
which is right for batch file transcription: full context and batched
decoding. Do not "fix" this by switching to the streaming variant.

The repo also carries `bpe.model`. **Do not add it — hotwords do not work
with this model.** It looked like the right fix for domain jargon, since
`create_stream(hotwords=...)` is per-stream and so costs nothing in the
recognizer cache. Tested against the real model:

| `modeling_unit` | Latin hotword | Chinese hotword |
|---|---|---|
| `bpe` | **segfault** | **segfault** |
| `cjkchar+bpe` | **segfault** | skipped, "Cannot find ID for token" |
| `cjkchar` | skipped | skipped |

The cause is in `tokens.txt`: it is a pure 5000-piece BPE vocabulary with no
bare CJK characters, so `cjkchar` lookups can never resolve, and the BPE
encode path crashes outright. A segfault takes the whole worker with it and
loses the entire job, which is far worse than the jargon it would fix. If a
later sherpa-onnx release fixes the crash, re-test all three units before
believing it.

## Accuracy levers that were tried and did not work

The room is the limit, not the post-processing. Measured on a real 69-minute
seminar recording: speech at -27 dBFS, level spread only 6.3 dB, almost no
clipping - but energy after a talker stops decays at only **28 dB/s, so
RT60 is roughly 2.1 seconds**. A typical meeting room is 0.4-0.6s. At 2.1s
every syllable smears into the next several, which is why English suffers far
more than Chinese: it leans on fast consonant transitions that the room
destroys.

Everything tried against that, all measured on the same audio:

| lever | result |
|---|---|
| GTCRN speech denoiser | **actively harmful**: 11/13 terms -> 6/13 |
| WPE dereverb (`nara_wpe`, taps 10/20/30) | **no effect**: 39 real English words -> 37/38/39 |
| `modified_beam_search` vs greedy | no term gain, +7% time, one Chinese fix in 11 windows |
| hotwords | segfault, see the X-ASR section |
| loudness normalisation | pointless - levels are already even |

The denoiser result is the instructive one: GTCRN is trained on *additive*
noise, and reverberation is not additive noise. Applied to a reverberant room
it strips the tails unevenly and takes speech with them. Do not reach for a
denoiser because the "SNR" looks low - on a reverberant recording that floor
*is* the previous syllable, not noise.

`scipy` and `nara_wpe` are deliberately **not** in `requirements.txt`. They
were installed to run the WPE test and removed again; it did not earn ~50 MB
of dependency.

What is left is the recording itself: a closer microphone. Direct-to-
reverberant ratio improves about 6 dB every time the mic-to-speaker distance
halves, and no amount of post-processing recovers consonants the room has
already smeared.

## Things that will bite you

- **The punctuation model cannot see marks that are already there.** It is
  trained on unpunctuated Simplified text, so a mark the recogniser emitted is
  invisible to it and it inserts its own right beside it. Cohere writes a
  half-width `?`, and a long sparsely punctuated line slips under the density
  gate, so real transcripts read `互相對於的？。` and `什麼 parameters。？首先`.
  `collapse_marks()` reduces any run of adjacent marks to the strongest one,
  *after* inference. Do not "fix" this by stripping punctuation before the
  model instead — that would have to strip `.` and `,`, turning `3.5` into
  `35` and changing the words, which the whole pass is forbidden from doing.
- **`.sent-play` is an `.icon-btn`, and the tools loop binds by class.** The
  per-sentence play button shares the `icon-btn` class with the row's tool
  buttons, so `$$('.icon-btn', el)` bound the rerun/split/merge/delete handler
  straight over the play handler and the button silently did nothing. The
  selector is scoped to `.sent-tools .icon-btn` for that reason.
- **The browser cannot play a project that has no `audio.wav`.** Obvious in
  hindsight, but a test fixture built by copying only `project.json` reports
  `NotSupportedError: The element has no supported sources`, which looks
  exactly like an autoplay-policy rejection. Check `audio.error` before
  concluding anything about user activation.

- **Simplified vs Traditional**: every model in the catalogue emits Simplified
  regardless of what was spoken, X-ASR included. `textproc.clean()` runs OpenCC
  (`s2twp` by default) and it is doing real work for all of them. This entry
  used to claim X-ASR emitted Traditional natively; it does not — decoding a
  sample with OpenCC switched off returns `我们一个平台两块`. Check a model's
  raw output before writing down what it emits.
- **Token-space spacing**: some models put a space between every token. `_CJK_SPACE`
  strips CJK↔CJK gaps only; spaces between Latin words must survive.
- **Special tokens leak out as literal text.** Both FireRedASR2 variants write
  `< sil >` — with the spaces, because they emit one token at a time — and it
  reached finished transcripts as if someone had said it. `_SPECIAL` in
  [app/textproc.py](app/textproc.py) is anchored on a known token list so a
  stray `<` in real speech survives, and `_DANGLING` cleans up the comma the
  removal strands (`嗯嗯嗯，< sil >。` → `嗯嗯嗯。`).
- **`secure_filename()` destroys a Chinese filename**, and with it the
  extension: `secure_filename("孟青宴安討論.m4a")` is `"m4a"`. The suffix then
  came out empty, which skipped the supported-format check (it only fired when
  a suffix was present), stored the upload as `source` with no extension, and
  named the project "m4a" - which the dialog hits for real, because it sends an
  empty name for every multi-file upload. `upload_name_and_suffix()` in
  [app/audio.py](app/audio.py) keeps the original name and takes the suffix
  from it. Sanitising bought nothing here: the upload is written to
  `source<suffix>` inside a uuid folder, so the user's filename never reaches
  the filesystem and only the whitelisted suffix does.
- **m4a/aac** cannot be read by libsndfile. [app/audio.py](app/audio.py) routes
  non-native suffixes through PyAV, which bundles FFmpeg — do not add a dependency
  on an external `ffmpeg` binary.
- **Segment length is capped, and must stay capped.** The VAD stops a chunk at
  `VAD_MAX_SPEECH_SEC`, but the safety net at the end of `split_by_speaker()`
  bypasses the VAD to recover speech pyannote found and the VAD missed — so it
  can emit a turn of any length. A 41-second unit reached the recogniser that
  way and crashed the graph outright (`Reshape ... Input shape:{1,41,16},
  requested shape:{-1,4081,4,4}` — 4081 frames is 40.8 s). Oversized units are
  now split into equal pieces at the end of that function.
- **One bad segment must not cost the whole meeting.** The batch decode falls
  back to decoding that batch one at a time and drops only what genuinely
  fails, logging the time range. Before this, a crash tens of minutes into a
  run lost everything.
- **`num_speakers`** matters a lot. Passing the real count (`num_clusters`) beats
  threshold-based auto-clustering by a wide margin; the UI asks for it up front.
- Windows PowerShell 5.1 only exposes the "Desktop" SAPI voices, so the test
  discovers voice names at runtime instead of hard-coding them.
- **Every promise that the UI awaits must settle on failure too.** `pollJob()`
  resolves on done/error/cancelled; an early version only resolved on success,
  which hung the upload flow forever when a download failed.
- Errors go to three places because a dialog cannot be copy-pasted: the server
  terminal (with traceback, via the `@app.errorhandler`), the browser console
  (`logError`), and a copyable dialog (`showError`). Route new failures through
  `showError`, not `toast`.
- `list_projects()` must not silently drop a project whose JSON it cannot read.
  `save()` swaps via `os.replace()`, and on Windows a concurrent reader hits
  PermissionError — projects flickered out of the sidebar while running.
  `_read_json()` retries; a genuinely unreadable project is listed as errored.
- **Anything that spawns the server with `stdout=subprocess.PIPE` must drain the
  pipe.** Left unread it fills after a few KB of logging and the server blocks on
  its next write — which presents as a total deadlock, requests timing out and
  jobs frozen mid-stage. `tests/test_e2e.py` runs a drain thread; `SERVER_LOG=1`
  echoes it. This cost a long debugging detour; do not reintroduce it.
- **Rankings are fetched live, never written by hand.**
  [app/leaderboard.py](app/leaderboard.py) reads the Open ASR Leaderboard from the
  Space's public Gradio config (`.../config`) - the results dataset itself is gated
  and needs a token, the Space config is not. Repo downloads/likes/license come
  from the Hub API. Both cache to `data/` for 12h and fail soft: stale cache, then
  nothing. Do not put rank or quality claims in `config.py`; `note` is for factual
  spec only, and a model with no entry is shown as "未列入", not guessed.
  That board is **English short-form**, so the Chinese models here legitimately
  have no entry.
- **ETA must not extrapolate from the fixed-cost stages.** decode and model-load
  do not scale with recording length; projecting the whole job from them gave a
  confident "2:23" for a 25-minute file. `_set()` times only `_PROPORTIONAL`
  stages and reports `eta: null` (UI: 估算中…) until 5% of that work is done.
- Some recognizers need a language: **Cohere Transcribe decodes every stream to
  empty text and still reports success if `language` is blank.** `resolve_language()`
  substitutes the model's default, and `transcribe()` raises when 5+ segments decode
  to nothing so a misconfiguration cannot silently save an empty transcript.

## Sample recordings

`tests/samples/` holds a real meeting (`孟青宴安討論.m4a`), a third-party
transcript of it (`.m4a.txt`) and one of ours (`08-25 規範討論.txt`), used to
compare models by hand. It is private content and gitignored; no test depends
on it.

## Models on disk

`models/` holds the shared ONNX files (VAD, segmentation, speaker embedding);
recognisers land in `models/hub/<key>/`, except SenseVoice, whose `dir` still
points at `models/sense-voice` so older installs keep working. The whole directory is
gitignored — roughly 1 GB bundled, and the catalogue can pull several GB more.

Because nothing under `models/` is in version control, a fresh clone has no models
at all. **`setup.ps1` downloads the four bundled files** from the sherpa-onnx
redistributions (SenseVoice, segmentation and the speaker embedding from Hugging
Face; silero_vad from the `k2-fsa/sherpa-onnx` GitHub release, which is the only
place its exact bytes are published). Each entry carries a verified byte count and
the download is rejected if the size does not match, so a truncated fetch cannot
masquerade as a working model. Files already at the right size are skipped, which
makes re-running `setup.ps1` the way to repair a partial install. If you change a
URL, re-check the byte count — that number is the only integrity check there is.
