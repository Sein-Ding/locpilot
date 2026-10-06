# LocPilot

> Click anywhere on the map, and your iPhone's location teleports there.

**Native macOS app** (SwiftUI + MapKit + system Liquid Glass) · an [iAnyGo](https://www.tenorshare.com/products/ianygo.html)-style iOS virtual location tool · iOS 17+ over a RemoteXPC/DTX tunnel, **no sudo** by default · **no jailbreak required**

[中文](README.md) | **English**

![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)
![Platform](https://img.shields.io/badge/platform-macOS%2026%2B-lightgrey.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)
![Status](https://img.shields.io/badge/status-v1.0.1-brightgreen.svg)

**Download**: [⬇️ latest installer (Apple Silicon / arm64, ZIP)](../../releases/latest)

> ⚠️ **Compliance notice**: this tool changes the **system-wide** location of a device. Use it only for your own testing, demos and development debugging. Do not use it to cheat, to bypass risk controls, or for any purpose that violates terms of service or applicable law.

## Overview

LocPilot turns "where is my iPhone right now" into a single click on a map: plug in the phone over USB, click anywhere, and the device location moves there — **no jailbreak, nothing installed on the phone**.

The UI uses system frameworks (SwiftUI + MapKit, i.e. Apple Maps itself) while the location work is delegated to mature open-source engines — **swap the engine without touching the UI, change the UI without touching the engine**.

* **Native macOS app**: the window holds exactly three things — the map, a control cluster top-right, a coordinate readout bottom-left. Full-bleed, with system Liquid Glass controls.
* **Python backend** (launched by the app, also runnable standalone): the engine adapter layer, exposing both an **HTTP/SSE API and a CLI** — multi-point routes, GPX playback, address search and history all live in the backend.

![LocPilot native UI: click the map to teleport](docs/images/app.png?v=1.0.1)

*Screenshot: Apple Maps itself, glass control cluster top-right, coordinate readout bottom-left; the pin shows the reverse-geocoded place name.*

### Key features

| Feature | Description |
|------|------|
| Native UI | Apple Maps + system gestures (two-finger pan / pinch), identical to the macOS Maps app; light/dark follows the system |
| Click to teleport | The only interaction. Top-right cluster = connection status / follow location / restore real GPS / zoom; **⌘K** connect · **⌘⇧K** disconnect · **⌘⇧C** restore |
| Pluggable engines | pymobiledevice3 (default) · libimobiledevice (iOS ≤16 only) · go-ios · mock, picked automatically per device and OS version |
| No-sudo tunnel | iOS 17+ uses RemoteXPC/DTX; the tunnel is established automatically as a normal user |
| Backend API + CLI | Shares one Session with the app: single teleport, multi-point routes, GPX import/export, speed and loop modes, address search, history |
| No Xcode required | `bash macos/build.sh` produces `LocPilot.app` with Command Line Tools alone |

**Stack**: Swift 5.9 + SwiftUI + MapKit + Liquid Glass (SwiftPM: LocPilotKit / LocPilotApp) · backend Python ≥3.9, standard library only at runtime · REST + SSE · engines pymobiledevice3 / libimobiledevice / go-ios / mock · geo MapKit / OSRM / Nominatim

**Status**: **v1.0.1** is feature complete, with a native Apple Silicon installer. Known limits: [section 7](#7-known-limits-read-first).

## 1. Interface

The main window holds exactly three things: the map, the control cluster in the top-right, and the coordinate readout in the bottom-left.

| Control | Purpose |
|---------|---------|
| 📱 Phone icon | Connection status: green glow once connected; click = connect / disconnect. Hover shows the device name |
| ➤ Locate arrow | Return to the current (virtual) position |
| ⤴ Restore real location | Clear the virtual location; the device returns to real GPS |
| + / - | Zoom in / out (relative to the currently visible span) |

**The gestures are the system map's gestures**: the window uses the real MapKit view as its canvas and enables only pan and zoom — **two-finger drag** to pan, **pinch** to zoom on the trackpad, with system inertia, rubber-banding and zoom anchoring, identical to the macOS Maps app.
This is what makes the tool feel **natural**: no joystick, no coordinate fields, no button panel — you just move the map with two fingers.

Shortcuts: **⌘K** connect · **⌘⇧K** disconnect · **⌘⇧C** restore real location.

> Complex features (multi-point routes, GPX playback, joystick, history, favorites, speed presets) live in the backend HTTP API and CLI; the native UI stays deliberately minimal.

## 2. Quick Start

### Option A — Download and run (recommended, no build)

**Requirements: an Apple Silicon (M1 / M2 / M3 / M4 …) Mac running macOS 26 or later** — this build targets the macOS 26 SDK so the system Liquid Glass appearance is enabled (an older SDK makes every system control fall back to the legacy look).

1. Open **[Releases](../../releases/latest)** and download `LocPilot-1.0.1-arm64.zip` (~470 KB)
2. Unzip it and drag **LocPilot.app** into Applications
3. **First launch**: this build is ad-hoc signed (not notarized), so Gatekeeper blocks the first double-click —
   right-click the app → **Open** → **Open** again; or run once:

   ```bash
   xattr -dr com.apple.quarantine /Applications/LocPilot.app
   ```

4. The engine is only needed to drive a **real** device: menu bar → *Engine → Install / Repair location engine…* (~40 MB, no sudo). Without it you can still explore the UI with the built-in mock device
5. Connect the iPhone over USB, tap "Trust This Computer", enable Developer Mode, click the phone icon in the top-right, then **click anywhere on the map** — the location moves there once the pin lands

> Verify the download: `shasum -a 256 LocPilot-1.0.1-arm64.zip` should match the SHA-256 shown on the Release page.

### Option B — Build from source

```bash
# 1) Install the location engine (creates .venv and installs pymobiledevice3; the app itself does not need it)
bash scripts/setup-engine.sh

# 2) Build the native app (SwiftPM, command line is enough, no Xcode required)
bash macos/build.sh              # output: macos/build/LocPilot.app
bash macos/build.sh --run        # build and launch

# 3) Open the app, click the phone icon in the top-right to connect, then click the map
```

No real device at hand? Try it with a virtual device:

```bash
LOCPILOT_ENGINE=mock bash macos/build.sh --run
```

Build script flags: `--check` (SwiftPM pre-check only) · `--selftest` (headless self-test, JSON) · `--smoke` (build + bundle structure check + launch smoke + crash guard) · `--run` · `--no-build` · `--help`.

**Environment variables** (honoured by both the app and the backend):

| Variable | Purpose |
|----------|---------|
| `LOCPILOT_ENGINE` | Force an engine: auto (default) / mock / pymobiledevice3 / libimobiledevice / go-ios |
| `LOCPILOT_AUTOCONNECT` | `0` = do not auto-connect on launch (for automation/acceptance, avoids touching a real device) |
| `LOCPILOT_PYTHON` | Force the Python interpreter (default probe order: app support directory → repo `.venv` → Homebrew → system) |
| `LOCPILOT_APP_SUPPORT` | Redirect the state directory (CI / portable mode) |
| `LOCPILOT_HOME` | Backend state directory (history, settings, runtime.json) |

## 3. CLI (shares the same Session as the app)

```bash
python3 -m locpilot engines --probe                  # engine availability and device list
python3 -m locpilot devices                          # connected devices
python3 -m locpilot set 31.2304 121.4737             # single-point teleport
python3 -m locpilot clear                            # restore the real location
python3 -m locpilot route --point 31.2304,121.4737 --point 31.2454,121.4987 --speed 6.9 --loop pingpong
python3 -m locpilot route --gpx track.gpx --speed 3  # GPX playback
python3 -m locpilot export-gpx --point 31.23,121.47 --point 31.24,121.48 --out route.gpx
python3 -m locpilot search "The Bund, Shanghai"      # address search
python3 -m locpilot doctor                           # environment self-check (JSON)
python3 -m locpilot serve --port 8799                # backend only (the app starts it automatically)
```

Common flags: `--engine {auto,pymobiledevice3,go-ios,libimobiledevice,mock}`, `--udid`, `--json`, `--offline`.

## 4. Engines & Requirements

| Engine | Works with | Tunnel | License | Notes |
|--------|-----------|--------|---------|-------|
| **pymobiledevice3** (default) | iOS ≤16 and 17+ | Required on iOS 17+ (established automatically, no sudo) | GPL-3.0 | Persistent worker session for high-frequency updates; CLI as fallback |
| libimobiledevice | **iOS ≤16 only** | Not required | LGPL-2.1 | Uses `com.apple.dt.simulatelocation`; that service is unavailable from iOS 17 on, and the engine refuses explicitly |
| go-ios | iOS ≤16 and 17+ | `ios tunnel start --userspace` | MIT | Alternative; measured conclusion: defer the switch, revisit if closed-source distribution is needed |
| mock | Any | — | This project | Device-free demos and automated verification |

Prerequisites (real device): USB connection with "Trust This Computer" accepted on the phone; **Developer Mode** enabled on iOS 16+
(`idevicedevmodectl enable` or `pymobiledevice3 amfi enable-developer-mode`, followed by a reboot and the lock-screen passcode); run `python3 -m locpilot doctor --probe` first to confirm.

If the automatic tunnel fails on iOS 17+ (for example iOS 17.0–17.3), start a shared tunnel manually and retry:

```bash
sudo .venv/bin/pymobiledevice3 remote tunneld     # root is only needed for shared/persistent tunnels
```
## 5. Architecture

```
macos/                     native app (SwiftPM project, command-line build, no Xcode required)
  Package.swift            two targets: LocPilotKit / LocPilotApp
  Sources/LocPilotKit/
    BackendController.swift  backend supervisor: interpreter probing, port selection, health checks, log forwarding, exit cleanup
    EngineClient.swift       REST + SSE client (status / connect / disconnect / teleport / clear / events)
  Sources/LocPilotApp/
    LocPilotApp.swift        app entry, menus, splash screen, --selftest entry
    AppState.swift           single source of truth (connection state / position / camera), @Published drives the UI
    MapScreen.swift          MapKit map + click-to-teleport + coordinate readout
    ControlsCluster.swift    top-right glass control cluster (system .glassEffect, material fallback on older systems)
    WindowConfigurator.swift full-bleed window (window changes deferred to the next runloop to avoid layout-time crashes)
locpilot/                  Python backend (driven by the app over HTTP, also usable standalone)
  cli.py / server.py / api.py / config.py
  core/     geo · route · playback · gpx · places · store · session
  engine/   base · pmd3 (with its persistent worker) · legacy · goios · mock
macos/build.sh               development build (produces LocPilot.app)
macos/dist.sh                distribution packaging (cleanup + re-sign + arm64 check + DMG + checksum)
docs/images/                 UI screenshot used by the READMEs
```

Key design decisions:

1. **UI and engine are decoupled**: the native UI only depends on `EngineClient`'s HTTP contract, so replacing an engine never touches the UI.
2. **A persistent worker instead of spawning a CLI per update**: pymobiledevice3's `simulate-location set` blocks on SIGINT, so restarting the process for every coordinate would rebuild the tunnel over and over; the worker opens the session once, then one line of JSON moves the position.
3. **The event stream drives the UI**: `/api/events` is SSE. Note that Foundation's `bytes.lines` **never emits empty lines**, so SSE frames must be split on raw bytes — otherwise the event stream dies silently (the UI looks fine while every update is dead).
4. **Never reconfigure the window during layout**: `WindowConfigurator` defers window configuration to the next runloop and **avoids all private KVC keys** — both approaches once caused launch-time crashes (SIGTRAP).
5. **Playback distance is geometric**: road distance returned by OSRM is reference data only; trusting it produces "progress 100% but the device has not arrived".

## 6. Build, Self-check & Packaging

```bash
# Native layer (zero-dependency test runner: Command Line Tools ship no XCTest)
swift run --package-path macos LocPilotTests

# Build + bundle structure check + launch smoke + crash guard (any new crash report fails the run)
bash macos/build.sh --smoke

# Headless self-test (JSON, for CI)
bash macos/build.sh --selftest

# Distribution package: strip local paths + ad-hoc re-sign + arm64 check + DMG + SHA-256
bash macos/dist.sh
```

Artifacts land in `macos/build/dist/`: `LocPilot-<version>-arm64.zip` + `.sha256` + install notes (DMG when the environment allows creating a disk image, automatic ZIP fallback otherwise).
The script enforces an arm64 binary and no local absolute paths inside the bundle, re-signs ad-hoc after touching Info.plist, then extracts the artifact to a scratch directory to re-verify the signature and `--selftest`.

> On a Mac with only Command Line Tools (no Xcode): `swift test` is unavailable (no XCTest), and SwiftUI macros such as `@State` cannot be used;
> SwiftPM needs `--disable-sandbox` plus a module cache redirected into the project.

## 7. Known Limitations (read first)

* **System-wide effect**: this changes the location of the whole device; it cannot be scoped to a single app.
* **The IP address does not change**: services that rely on IP/Wi-Fi positioning still see the real region.
* **Detectable**: CoreLocation exposes `isSimulatedBySoftware`, so games and risk-control systems may recognise simulated locations; do not use this to break terms of service or to cheat.
* **On iOS 17+ the location lives with the connection**: dropping the DVT connection restores the real location, so the app / service must stay running.
* **On iOS ≤16 the location is device-side state**: it survives process exit and only `clear` (or a reboot) restores the real location.
* Developer Mode and a Developer Disk Image are required; locked or untrusted devices cannot work.
* The native map is provided by MapKit and needs network access (Apple Maps services). In China the data source is AMap, but coordinates stay WGS-84 — **no GCJ-02 correction is needed**.

## 8. Troubleshooting

| Symptom | What to do |
|---------|------------|
| No device found | Try another cable / USB port; tap "Trust" on the phone; `python3 -m locpilot doctor --probe` |
| Tunnel error on iOS 17+ | Run `sudo .venv/bin/pymobiledevice3 remote tunneld`, or pass `--rsd HOST PORT` |
| `not supported on iOS 17+` | You are on the libimobiledevice engine; switch back with `--engine pymobiledevice3` |
| Crash on launch | Check `~/Library/Logs/DiagnosticReports/LocPilot*`; two historical causes: private KVC keys, and window changes during layout — both fixed and guarded by the smoke test |
| UI says "connected" but the status never updates | Event-stream problem: make sure `EngineClient.events()` frames on bytes (`bytes.lines` swallows SSE empty lines) |
| Playback pauses after a failed coordinate update | Check the backend log and `engine.error`; reconnect the device and continue via CLI / API |
| `PermissionError: …/.pymobiledevice3` | Home directory is not writable (sandbox / CI / read-only HOME). The engine falls back to `<state dir>/pmd3-home`; you can also set `LOCPILOT_PMD3_HOME=/writable/path` explicitly |

## 9. Security

Listens on `127.0.0.1` only by default; `/api` can require a `--token`; static assets are protected against path traversal.
This tool runs device-control commands on your machine, so never expose the port to the public internet.

## 10. State Directory & Logs

* State directory: `~/Library/Application Support/LocPilot` (`runtime.json` records pid / port / interpreter)
* Backend logs: the app forwards backend stdout/stderr; when run standalone they are visible directly in the terminal
* The "Engine → Open State Directory" menu item opens it in Finder

## 11. License & Credits

LocPilot is released under **GPL-3.0**: the default engine pymobiledevice3 is GPL-3.0, and this project builds on it and imports its Python API.
Switching to the go-ios (MIT) engine and removing the pymobiledevice3 adapter would allow a permissive license.
Third-party attribution is in [NOTICE.md](NOTICE.md).

## 12. Changelog

What each release fixed and added lives in **[CHANGELOG.md](CHANGELOG.md)** (Chinese); installers and SHA-256 are on [Releases](../../releases).

**v1.0.1** fixed: misplaced hover detection at the window top, traffic lights not revealing `× − +` on hover, the first `/api/status` call blocking for ~14s, the stray dash in the bottom-left readout, and moved system controls to the current macOS appearance.

---

**Questions / bugs / ideas**: please open an [Issue](../../issues) so others can find the answer too.
