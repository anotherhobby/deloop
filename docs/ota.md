# deloop OTA Reference

See [docs/architecture.md](architecture.md) for the overall project layout, and
[docs/device-drivers.md](device-drivers.md) for the unrelated device-backend architecture.

## Self-Update (OTA)

`src/ota.py` -- deloop can update its own app files over Wi-Fi from a flat S3 layout,
manual-check-only (no background polling), via a top-level **Update** menu entry
(`config.OTA_ENABLED`, on by default). **Never touches CircuitPython firmware** -- only the app
files `make deploy` already ships. Orthogonal to `DEVICE_DRIVER`, same category as the "MENU home"
chrome: no backend-specific code anywhere in this feature.

## Architecture: live remount + soft reload, never a hard reset

`storage.remount("/", readonly=False)` is called live, with zero reboot, as long as CIRCUITPY
isn't actively host-mounted -- the one real dependency this feature has, surfaced to the user as
the Update menu's `Eject drive first` result rather than failing silently.

**Hard rule: never call `microcontroller.reset()` anywhere in the OTA flow, including for test
setup.** A genuine hardware reset breaks the first post-handshake TLS `send()` on the very next
boot's first network attempt -- confirmed via raw-socket diagnostics that TCP connect and the TLS
handshake itself both succeed, only the first post-handshake `send()` fails, 100% reproducibly,
specifically following a hard reset earlier in the same power cycle. `supervisor.reload()`
(CircuitPython's own soft-reboot, same as Ctrl-D in the REPL) does not have this problem. If a
clean NVM state is needed for testing, use a true power cycle (unplug/replug), never
`microcontroller.reset()`.

`ota_boot.py` is a separate module from `app.py`, and `code.py` imports it instead of `app.py`
whenever an OTA action is pending -- `import app` alone unconditionally pulls in the entire
device-driver stack (`driver.py`, `dial_ui.py` and its loaded PCF fonts, `sound.py`, `state.py`),
which leaves too little free memory for a reliable TLS handshake. `ota_boot.py`'s only display
dependency is `terminalio`'s built-in font plus `vectorio` for a plain background rect -- no PCF
font loading, unlike `dial_ui.py`.

**NVM scheme** (byte 2 = action, byte 3 = result, byte 4 = latest-detected version -- bytes 0/1
are brightness/sound, defined once in `config.py` since both `app.py` and `ota_boot.py` need it):
- `NVM_OTA_ACTION`: `0` idle, `1` pending check, `2` pending install. Set by `app.py`'s Update
  submenu; read once at the top of `code.py`/`ota_boot.run()`, cleared immediately after reading
  (before any network work) so a failure later can never boot-loop the device.
- `NVM_OTA_RESULT`: `0` none, `1` available, `2` up_to_date, `3` check_error, `4` install_ok,
  `5` install_failed, `6` eject_needed. Set by `ota_boot.run()` right before its own reload back to
  normal mode; read once on the very next normal boot, surfaced into `loop.ota_status`, then
  cleared -- survives exactly one reload boundary, never more.
- `NVM_OTA_VERSION`: the latest version a successful check found, so the Update menu can show
  `Install Update (vN)` without a live network call just to render a menu label.

## Install Update requires a genuine physical power cycle

Check Now reloads immediately (it only ever makes one request, to `latest.json`, and has been
reliable that way). Install Update does **not** self-reload: tapping "Install" sets
`NVM_OTA_ACTION = 2` and shows "Power cycle now to install vN" / "Cancel" instead, and the actual
install only runs on the next genuine power-on.

Why: ESP-IDF's real WiFi stack bring-up (`esp_wifi_init()`, netif creation, event-handler
registration, in `ports/espressif/common-hal/wifi/__init__.c`) is gated by a plain `static bool
wifi_inited` that is never reset by `supervisor.reload()` -- it only ever runs once per power
cycle, the first time anything touches `wifi.radio`. Every soft reload after that just flips the
already-initialized radio back on and reuses whatever that first init produced. A multi-request
network sequence like Install Update is more reliable against a freshly-initialized radio than one
that's been soft-reloaded repeatedly in the same power cycle, so the design trades one extra user
step (a real power cycle) for that clean state. `code.py` already reads `NVM_OTA_ACTION` before
deciding whether to `import app` or `import ota_boot`, so leaving `action = 2` set is enough -- the
next real power-on picks it up automatically.

The install screen shows live progress ("Updating to vN... X of 16 packages"), updated right
before each file's own download starts (via `apply()`'s `on_progress` callback) -- so a stalled or
crashed install shows the file it actually died on, not the last one that already finished.

## Versioning: fully automatic, never hand-chosen

`.github/workflows/release.yml` triggers on push to `main`, filtered to `src/**`,
`tools/build_release_manifest.py`, and the workflow file itself (plus manual
`workflow_dispatch`) -- not on a pushed tag, and not on docs/README-only pushes, which don't change
anything that ships to the device. It computes the release version as **`(highest existing vN
tag) + 1`**, always, then `gh release create vN --target $GITHUB_SHA --generate-notes`. `v1`-`v4`
predate this workflow (hand-created, no assets) and are handled fine -- the version step just sees
them as ordinary existing tags and continues from `v5`. A `concurrency: group: deloop-release`
guard serializes runs so two near-simultaneous triggers can't compute the same "next version" and
race to create the same tag.

**Never manually create a `vN` GitHub Release/tag for this repo -- merging to `main` is the only
version-numbering path**, or two different mechanisms will race to pick "the next version."

The workflow bakes `CURRENT_VERSION` into `src/version.py` as a working-copy-only step (never
committed back), compiles every module in `MPY_MODULES` with a pinned, version-matched Linux amd64
`mpy-cross`, and builds `manifest.json` (sha256 + size per file) via
`tools/build_release_manifest.py` (an explicit file allowlist, kept in sync by hand with the
Makefile's `MPY_MODULES`).

### Why local builds have no version, and why it stays that way

`src/version.py` holds `CURRENT_VERSION = 0` in git. The workflow overwrites it in its own working
copy only, so **every `make deploy` is version 0** -- including a clone checked out at the exact
release tag. `check_latest_version()` then returns `(N, 0)`, a fresh install always sees an update
available, and installing it pulls the real number in `version.mpy`.

The Update screen shows **nothing** for version 0 rather than "v0" (see `app.py`'s
`_ota_version_text()`): a local build is implied by the absence of a release version, and "v0"
reads as a bug on a new user's first screen.

Two rejected alternatives, both of which break the same invariant -- **a build must never claim to
be a release it isn't**:

- **Stamp the version from the git tag at `make deploy` time.** A tree with local edits, or main a
  few commits past the tag, would report `vN` while running something else. Worse, it fails
  dangerously: `check_latest_version()` compares `N < N`, which is false, so the update prompt is
  *suppressed*. A device running unreleased code, convinced it is current. `0` means "no release
  provenance" -- true, and self-healing after one update.
- **Have the release workflow commit `version.py` back to main.** Same defect, since most people
  clone main's HEAD rather than a tag, so the committed number is stale for everyone who arrives
  after the next merge. It also recurses: `src/**` is in the workflow's trigger paths, so the
  commit-back would mint another release, forever. Tags already make the repo self-describing.

The forced first OTA is a feature, not a wart, for two reasons. It replaces whatever the local
`mpy-cross` produced with the CI-built, manifest-verified assets. And it means **every release is
validated by its own author**: cutting a release makes the maintainer's device the first to
attempt the upgrade, so the OTA path is exercised end to end on every single version. Which path
gets exercised depends on what that device last did -- after an OTA it genuinely reports `vN` and
tests the real `vN -> vN+1` upgrade a user would take; after a `make deploy` it is back to 0 and
tests `fresh -> latest` instead. Both are worth having; know which one you ran.

Note that OTA covers the app files only (`_MPY_MODULES` + `code.py`). Fonts, sounds and
`splash_logo.bmp` are deliberately excluded from the manifest, so a device cannot be bootstrapped
from OTA alone -- `make deploy` is always the first step, and "fresh install, then update" is the
intended path.

## Where releases are published

Two places, for two different audiences:

- **S3** (`config.OTA_S3_BASE`, a flat bucket layout) -- what the device actually fetches. Every
  release's compiled files and `manifest.json` land under `s3://<bucket>/vN/`; `latest.json`
  (`{"version": N}`) at the bucket root is written **last**, only after every versioned asset has
  finished uploading, so a device can never observe "latest" pointing at a partially-uploaded
  version. Every request -- `latest.json`, `vN/manifest.json`, every `vN/<file>` -- lands on this
  one host directly, no redirects.
- **GitHub Releases** -- changelog/history only. `release.yml` still creates a Release and uploads
  the same assets there via `gh release create`/`gh release upload`, so there's a browsable history
  with auto-generated release notes. The device itself never talks to GitHub; `ota.py` only ever
  fetches from S3. `tools/probe_ota.py` (`make probe-ota`) is a host-side sanity check for this
  GitHub Release's shape, independent of the device's real update path.

CI authenticates to AWS via OIDC (`aws-actions/configure-aws-credentials`, assuming
`arn:aws:iam::223166783801:role/GHADeloop`) -- no long-lived keys stored in the repo.

## `ota.py`'s session/connection handling

Covered in detail in `src/ota.py`'s own docstrings (the `_Fetcher` class, `_reset_connections()`,
`_maybe_reset()`) -- read those before touching retry/session logic. In short: `_Fetcher` owns one
`adafruit_requests.Session`, rebuilt from scratch (fresh wifi connect, fresh socket pool, fresh TLS
context) on any retry or whenever the target host changes. Every request from a real Check
Now/Install Update lands on the same host (`config.OTA_S3_BASE`), so in practice this only ever
rebuilds once per real operation, plus on a retry.

## Known limitations, deliberately deferred

- **Idle-socket retention between sequential requests** is a small, quantified memory cost (not
  fragmentation -- genuine object lifetime) that doesn't matter for the real product flow: a real
  Install Update is exactly one sequence per boot, and that passes cleanly. It only shows up when
  the full sequence is repeated multiple times without a reboot in between, which production never
  does. Force-closing idle sockets after every request would likely address it, but changes
  connection-lifecycle semantics in a lower-level networking component -- deliberately kept
  separate rather than bundled in, so a future regression is easy to attribute to one change or the
  other.
- No built-in CircuitPython feature covers this use case. `adafruit/circuitpython#3777`'s "User
  Code Update" half (updating app files, not firmware) was explicitly punted to "a library... not
  the core," and nobody has built one. Web Workflow (`supervisor/shared/web_workflow/`) is the one
  shipped feature in this space, but it's a human/host pushing files over the network, not a device
  autonomously checking a remote release and installing it unattended.

## Testing

- `tools/ota_regression_check.py` (`make probe-ota-regression`) -- runs the real
  latest.json-fetch -> manifest-fetch -> per-file download+verify sequence on real hardware, over
  real Wi-Fi, using whatever `ota.py`/`ota_boot.py` is currently deployed. Stops short of the final
  rename-into-place commit, so a pass never overwrites the files actually running on the device.
  Run this after any change to `ota.py`'s `_Fetcher`/session/`apply()` internals -- it's far faster
  to iterate on than a full manual Install Update cycle, though it's not a substitute for testing
  the real Check Now/Install Update menu items end to end at least once.
- `tools/probe_ota.py` (`make probe-ota`) -- host-side sanity check for the GitHub Release
  `release.yml` publishes (see "Where releases are published" above).
- `make build-manifest` -- builds `manifest.json` locally from whatever `make deploy` last
  compiled into `local/build/`, for sanity-checking `tools/build_release_manifest.py` before it
  runs for real in CI.

### The release ritual: validate the real upgrade path, not the fresh-install one

Cutting a release already forces the author to be the first to attempt the upgrade (see "Why local
builds have no version" above). Sequencing it deliberately makes that a much better test:

1. **Finish testing the change** the normal way -- `make deploy`, exercise it on hardware. The
   device is now version 0.
2. **Run Check Now / Install Update before merging.** From 0 the only thing installable is current
   latest, i.e. the *previous* release `vN`. The device is now genuinely on `vN`.
3. **Merge to main.** CI cuts `vN+1`.
4. **Run Check Now / Install Update again.** This exercises the real `vN -> vN+1` upgrade a user
   would take, rather than the `fresh -> latest` path a dev machine normally tests.
5. **Check the version row reads `vN+1`.** This is the only step that confirms `version.mpy`
   itself shipped correctly -- everything else would pass just as well with a stale one.

Two caveats worth holding:

- Between steps 2 and 4 the device runs the *previous* release, not the code you just tested. Do
  all hands-on validation before step 2.
- The artifact you end up running was compiled by CI's `mpy-cross`, not the local one in
  `local/mpy-cross`. They are pinned to the same CircuitPython version (`CIRCUITPYTHON_VERSION` in
  `release.yml`, and the Makefile's own comment) but that pinning is maintained by hand -- if they
  ever drift, step 4 is the first place it would show up, which is another reason to run it.
- If a release is broken badly enough to prevent OTA, there is no OTA path back. `make deploy`
  over USB is always the recovery.
