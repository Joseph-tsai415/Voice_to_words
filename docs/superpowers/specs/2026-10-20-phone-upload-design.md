# Phone upload by QR code — design

Date: 2026-10-20
Status: draft, awaiting approval

## Intent

Meetings are recorded on a phone (a recorder app that saves into the iPhone
Files app, or the Android equivalent). Today the only way into the tool is the
laptop's own file picker, so the recording has to reach the laptop some other
way first. The user wants to scan a QR code shown on the laptop with the
phone, pick the file in the phone's browser, and have it arrive on the laptop.
Nothing else about starting a transcription changes: the file shows up in the
新增會議 dialog and the user sets speaker count, model etc. there as now.

"Simple" is the user's word and the governing constraint: scan, pick, done.
No app to install, no account, no pairing ritual.

## Goals

- A 用手機傳 option in the existing 新增會議 dialog that shows a QR code.
- Scanning it with the stock camera (iPhone or Android) opens a page in the
  phone's browser with one button that opens the phone's own picker (Photos /
  Videos / Files on iOS). Picking one or more files sends them to the laptop
  with a visible progress bar.
- Each received file appears in the open dialog, marked 來自手機, and is
  started with the dialog's normal 開始辨識 button and settings.
- **Each QR code works for one phone only** (user's requirement: "unique to
  one device, prevent lots of people scanning it"). A second device that scans
  the same code is refused.
- Works on the same Wi-Fi, and on the laptop's Windows Mobile Hotspot with the
  phone joined to it (that is the same situation from the app's point of view).

## Non-goals

- Starting a transcription from the phone. The phone only sends files; no
  model / speaker settings on the phone page.
- Browsing, playing or editing transcripts from the phone.
- Turning on the Windows hotspot automatically, or any connection method that
  is not "both devices on one network" (Bluetooth, USB, cloud relay).
- HTTPS. Plain HTTP on the local network; see Constraints.
- A native app or PWA install.

## Anti-goals (would make this a failure even if it technically works)

- **Exposing the meeting library to the network.** The app today listens on
  `127.0.0.1` only. On an office Wi-Fi, anything that makes transcripts,
  audio, edit or delete endpoints reachable by other machines is a leak of
  private meeting content. The phone feature must not widen that.
- A QR code that keeps working for whoever else scans it (a colleague
  photographing the screen, a screenshot passed around).
- Losing a file that already finished transferring because the dialog was
  closed by accident.
- Silent failure on the phone: a page that spins forever when the laptop is
  unreachable or the upload dies.

## Constraints

- UI language is Traditional Chinese, phone page included.
- Fully local, offline-capable: no CDN, no external service. A QR library must
  be a pip dependency or a vendored file.
- Windows Defender Firewall prompts the first time `python.exe` listens on a
  non-loopback address; the user must click 允許. On a network Windows has
  classified as **Public**, inbound is blocked unless allowed for public
  networks. The laptop UI has to say this plainly, because the symptom on the
  phone is just "page won't load".
- Some office/hotel Wi-Fi isolates clients from each other; same symptom. The
  hint should point at the laptop hotspot as the way around it.
- Plain HTTP means the page is not a *secure context*: no Wake Lock, no
  service worker. Don't design around APIs that need one.
- Memory/size: no artificial caps (project rule). A phone video of a meeting
  can exceed the main app's 2 GB `MAX_CONTENT_LENGTH`; the phone path should
  stream to disk rather than buffer, and not impose its own cap.
- `requirements.txt` changes re-run the install on next launch (the stamp
  hashes it). That is fine and expected; `setup.ps1`/`setup.sh` need no change
  for a pip package.

## How it works

### Decided by the user

- Flow: laptop shows QR → phone camera → phone browser page → pick from
  Photos / Videos / Files → file transfers → file appears on the laptop →
  user finishes setup in the 新增會議 dialog and presses 開始辨識.
- Both devices on the same Wi-Fi (or the laptop's hotspot).
- One QR code = one phone.
- Keep it simple.

### Decided by me (Claude), open to correction

1. **A separate listener, not the main server on `0.0.0.0`.**
   The main app stays bound to `127.0.0.1` exactly as now. Opening the
   用手機傳 tab starts a second, small WSGI server (Werkzeug `make_server`,
   threaded, in-process) bound to the LAN, serving **only** the phone routes
   from its own minimal Flask app. It shuts down when the phone session ends.
   Reason: a route guard on a LAN-bound main app is one bug away from
   exposing every transcript; a listener that has no such routes cannot leak
   them, and nothing listens on the network at all while the feature is not
   in use. Port: any free port (the firewall rule is per executable, not per
   port).

2. **Phone session lifecycle.**
   - Laptop opens the tab → `POST /api/phone/session` (main app) creates a
     session `{token, created, device: None}` with a fresh
     `secrets.token_urlsafe` token, starts the listener if needed, and returns
     the phone URL `http://<lan-ip>:<port>/u/<token>` plus the QR as SVG.
   - Only one session exists at a time; creating a new one invalidates the old.
   - **One device:** the first page load of `/u/<token>` claims the session
     and sets a random per-device cookie (store its hash). Later requests must
     carry the cookie. Any other device gets a plain refusal page:
     此 QR 碼已在另一台裝置使用。請在電腦上重新產生。
     Strictly this is one *browser*, not one phone: someone who scans with
     LINE's reader (common in Taiwan), lands in LINE's in-app browser, then
     taps "open in Safari" is refused in Safari. That is accepted, to keep
     the guarantee strong — LINE's in-app browser can pick and upload files
     itself, so staying there works. The way out is on the laptop: once
     claimed, the dialog shows 已連線 (with a rough device/browser label from
     the User-Agent) and a **換一個 QR 碼** button that ends the session and
     shows a fresh code. Claiming on first *upload* instead was considered and
     rejected: a second person could then open the page and race the owner.
   - The claimed phone may send several files for as long as the session
     lives.
   - The session ends when the dialog closes or the tab is switched away
     (`DELETE /api/phone/session`), **or** when the laptop page stops polling
     for ~60 s (tab closed without closing the dialog). The dialog already
     polls for received files, so the poll doubles as a heartbeat.
   - An upload already in progress when the session ends is allowed to
     finish; no new uploads are accepted. The listener stops once idle.
   - Re-opening the tab always makes a new code.

3. **Received files are held on the laptop until used.**
   Uploads stream to `data/incoming/<id>/<original-name>.part` and are renamed
   on completion. The dialog lists every completed incoming file, including
   ones left from an earlier, accidentally closed dialog, each with a × to
   discard. On 開始辨識, each incoming file is turned into a project by the
   same code path as a normal upload, except the file is moved (`replace()`,
   same volume) into the project folder as `source<suffix>` instead of being
   re-sent from the browser. `upload_name_and_suffix()` and the
   `SUPPORTED_SUFFIXES` check apply exactly as for a laptop upload. Anything
   not supported is rejected on the phone, before transfer, with the same
   message wording.

4. **QR generated server-side with `segno`** (pure Python, no dependencies),
   returned as inline SVG. The laptop dialog also shows the URL as text so it
   can be typed if scanning fails.

5. **LAN address, and what the listener binds to.**
   - **Hotspot first.** If the Windows Mobile Hotspot is up (its adapter holds
     a `192.168.137.x` address by default), the QR carries that address. The
     hotspot shares an existing connection, so a default route through the
     office Wi-Fi normally still exists while it is on, and choosing by
     default route would put the office address in the QR. The office
     address is exactly the one the hint sent the user away from. Someone who
     turned the hotspot on is using it for this.
   - Otherwise use the interface that holds the default route (UDP `connect`
     trick, no packet sent), falling back to the first private IPv4.
   - Ignore loopback, link-local, and virtual adapters (WSL / Hyper-V
     `vEthernet`; this machine has `vEthernet (WSL) 172.24.80.1`).
   - One QR, no address picker. The URL is also shown as text.
   - The phone listener binds **all interfaces** (`0.0.0.0`), so whichever
     network the phone is actually on can reach it. That is safe because of
     call 1: the listener serves only the phone routes, each gated by the
     session token and the one-device cookie.

6. **Phone page.** One standalone template, mobile layout, app colours.
   Title 傳送錄音到電腦, one large 選擇檔案 button
   (`<input type="file" multiple>` with an `accept` built from
   `SUPPORTED_SUFFIXES`), per-file progress via `XMLHttpRequest`
   (`fetch` has no upload progress), files sent one after another, a
   請保持此頁開啟 note, a clear ✓ 已傳到電腦 on completion and a readable
   error with a 重試 on failure. The upload body is the raw file
   (`xhr.send(file)`), name in a header, so the server can stream it without
   multipart parsing.

7. **Laptop dialog states.** 用手機傳 tab shows: QR + URL + the network hint
   (same Wi-Fi; first-time firewall prompt → 允許; if the phone can't open it,
   office Wi-Fi may block it → use this laptop's hotspot). Status line
   progresses 等待手機掃描 → 已連線 → 接收中「檔名」 → received files listed.
   開始辨識 is enabled when there is at least one laptop-picked or
   phone-received file; both kinds can be mixed in one submission.

8. **開始辨識 waits for the phone to finish.** Phone files go one after
   another, so with three picked, the first lands well before the third.
   Today `#new-start` closes the dialog immediately (`app.js` `initNewDialog`),
   and closing ends the session. So pressing it early would silently leave
   the rest out, or refuse them. Instead:
   - When the user picks files, the phone first tells the server how many are
     coming.
   - While any are outstanding, 開始辨識 is disabled and the status reads
     接收中 2/3，傳完才能開始.
   - A transfer that fails clears its outstanding count, so the button cannot
     stay disabled forever. The phone shows the error and 重試.
   - Starting late arrivals automatically with the submitted settings was
     considered and rejected. It needs a server-side record of pending
     settings and breaks "you finish setting it up on the laptop".

## Testing

A self-contained `tests/test_phone.py` in the style of the others (no audio,
runs in seconds):

- a session's first device claims it; a second device is refused, on the page
  and on the upload endpoint;
- a new session invalidates the old token;
- an upload streams to `incoming/`, a `.part` never shows as received, and an
  unsupported suffix is refused;
- creating a project from an incoming file moves it into the project as
  `source<suffix>` and leaves nothing in `incoming/`;
- the phone app's URL map contains only the phone routes (no `/api/...`), so
  the LAN listener cannot serve the library;
- the session expires without heartbeats;
- 換一個 QR 碼 invalidates the claimed token, and the new one is claimable;
- with outstanding files announced, the session reports "not ready to start"
  until they all arrive or fail;
- address selection prefers a `192.168.137.x` interface when one is present
  and never returns a `vEthernet` / loopback / link-local address. Test it
  with a fake interface list; don't depend on this machine's adapters.

Plus one manual check on a real iPhone and Android phone: the picker offers
Photos/Videos/Files, an `.m4a` from Files and a `.mov` from Photos both
arrive, and a second phone scanning the same code is refused.

`test_e2e.py` should still pass unchanged (the main server's binding and
upload path are untouched apart from the incoming-file branch).

## Left to the builder

- Exact route names and JSON shapes.
- Whether the laptop shows byte-level progress for an incoming upload or just
  接收中 (byte progress is a nice-to-have, not required).
- How the incoming list is cleaned of `.part` files from a server killed
  mid-transfer (keep non-empty ones only if resumable; otherwise remove at
  startup like `hub.cleanup_partials()`).
- The heartbeat timeout value and polling interval.
- CLAUDE.md: add a section explaining the separate listener and the
  one-device rule, so nobody "simplifies" it into binding the main app to
  `0.0.0.0`.
