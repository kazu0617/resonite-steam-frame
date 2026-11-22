# How it works

This document describes what `install.sh` and `src/resonite_frame.py` do on Steam Frame, based on the code. For usage, see [README](../README.md). The Japanese version is [how-it-works.ja.md](how-it-works.ja.md).

- Target version: `VERSION = "0.3.2"` (`src/resonite_frame.py`)
- Target app: Steam App ID `2519830` (Resonite)

## Overview

The Steam version of Resonite bundles a Windows build of the game and a Linux launch script (`LinuxBootstrap.sh`). Steam Frame is an ARM64 Linux device, so two things do not line up out of the box:

1. The launch script expects an x86-64 .NET runtime.
2. The native libraries used for model import, image conversion, and video playback assume Windows / x86-64.

The installer replaces Steam's launch path, places an ARM64 .NET runtime and ARM64 native libraries inside the game folder, and rewrites the launch script and Steam settings so that the Linux launch path runs natively on ARM64 under Proton Experimental. Every change is recorded in an installation manifest, so `--uninstall` can put everything back.

## Flow

When run with `--install`, the installer proceeds roughly as follows.

1. Validate preconditions (Resonite not running, Steam not updating, required commands and Proton Experimental present).
2. Validate that `LinuxBootstrap.sh` can be rewritten before doing anything else.
3. Read Steam's current launch options and compatibility tool setting.
4. Prepare the following in a temporary directory:
   - host ARM64 libraries
   - ARM64 .NET runtime
   - Mikktspace build
   - yt-dlp and Deno
   - Compressonator build
5. Re-check that the game and Steam settings did not change during preparation.
6. Apply the game files, launch helper, and Steam settings as a transaction.
7. Mark the manifest as `installed`.

All preparation happens in a temporary directory; writes into the game folder are batched into step 6.

## 1. Replacing Steam's launch path

`launch_options()` rewrites Resonite's launch options to:

```
<absolute path to helper> %command% -Device SteamVR
```

The helper is created at `~/.local/bin/frame-resonite-launch` by default (`--bin-dir` changes it). It is a small Python script that adds `~/.local/share/resonite-steam-frame/lib` to `sys.path` and calls `resonite_frame.launch()`.

Before executing the command Steam passed, `launch()` verifies:

- the manifest status is `installed`;
- every required file recorded at install time exists;
- `Renderite.Host.frame.runtimeconfig.json` matches what is derived from the game's `Renderite.Host.runtimeconfig.json`;
- an ARM64 .NET runtime satisfying the requirement exists in the game folder;
- `LinuxBootstrap.sh` is patched exactly as recorded.

It then runs the command with `PROTON_SET_GAME_DRIVE=0` and `FEX_TSOENABLED=1` added to the environment. The latter enables FEX's x86 TSO emulation.

If the existing launch options already contain a custom `%command%` wrapper or anything the installer cannot interpret, it fails instead of guessing.

## 2. Rewriting `LinuxBootstrap.sh`

`patch_bootstrap()` only accepts a known script shape and changes four things:

| Item | Change |
| --- | --- |
| .NET location | `DOTNET_ROOT` from `$PWD/dotnet-runtime` to `$PWD/dotnet-runtime-arm64` |
| dotnet-install arguments | pin to `--version <required version> --runtime dotnet --architecture arm64` |
| OpenSSL and library search | point `OPENSSL_MODULES`, `OPENSSL_CONF`, `LD_LIBRARY_PATH` at `frame-libs-arm64` |
| Engine startup | `dotnet exec --runtimeconfig "$PWD/Renderite.Host.frame.runtimeconfig.json" Renderite.Host.dll "$@"` |

The patch is idempotent. It verifies that each target string occurs exactly once before replacing it, and stops with `bootstrap_unsupported` if the shape has changed.

## 3. ARM64 .NET runtime

`runtime_requirement()` reads the game's `Renderite.Host.runtimeconfig.json` and determines the required .NET version and roll-forward policy. Prerelease versions and `rollForward: disable` require an exact match; otherwise the installer picks the latest patch in the same major/minor series.

`stage_runtime()` prepares the runtime as follows.

1. Reuse an ARM64 runtime in `dotnet-runtime-arm64` if it satisfies the requirement (no network needed).
2. Otherwise fetch `https://builds.dotnet.microsoft.com/dotnet/release-metadata/<major.minor>/releases.json`.
3. Select `dotnet-runtime-linux-arm64.tar.gz` for `linux-arm64`.
4. Verify the URL is HTTPS on a Microsoft host and that a SHA-512 hash is present.
5. Download, verify SHA-512, and unpack.
6. Confirm that `dotnet` and `libcoreclr.so` are ARM64 ELF files.

The runtime is installed into `dotnet-runtime-arm64/` in the game folder.

## 4. Normalizing the Windows runtime config

The game's `Renderite.Host.runtimeconfig.json` may include `Microsoft.WindowsDesktop.App` for Windows desktop. `linux_runtime_config()` reads it and generates `Renderite.Host.frame.runtimeconfig.json` containing only `Microsoft.NETCore.App`.

- The original `Renderite.Host.runtimeconfig.json` is left untouched (to preserve Steam's Windows configuration).
- Unexpected framework names or duplicates cause an error.
- The generated config is recorded as a required file and checked at launch and during `--check`.

## 5. Collecting host ARM64 libraries

`stage_host_libraries()` collects the following libraries and their dependencies from `/usr/lib` and `/usr/lib/aarch64-linux-gnu` on SteamOS and copies them into `frame-libs-arm64/`.

- ICU: `libicuuc.so`, `libicui18n.so`, `libicudata.so`
- OpenSSL 3: `libssl.so.3`, `libcrypto.so.3`
- PulseAudio: `libpulse.so.0`, `pulseaudio/libpulsecommon-*.so`
- OpenSSL legacy provider: `ossl-modules/legacy.so`

Dependencies are followed recursively using `ldd`. Core libraries (`CORE_LIBS`, such as `libc.so` and `ld-linux`) and the loader's own absolute path are excluded. Each collected file must be an ARM64 ELF, and any `not found` reported by `ldd` is an error.

The installer also generates `openssl.cnf`, which activates the legacy provider; the game's launch script references it through `OPENSSL_CONF`.

## 6. Mikktspace (FBX model import)

Resonite uses Mikktspace for FBX import. `stage_mikk()` fetches Yellow Dog Man's `Mikktspace.NET` at a pinned commit (`a25b708d…`), verifies its SHA-256, and builds `mikktspace.c` and `mikktspace_api.cpp` with `c++ -O3 -fPIC -shared` into `runtimes/linux-arm64/native/libmikktspace.so`.

After building, `probe_mikk()` uses ctypes to call `genMikktTangSpace()` on a known triangle and checks the callbacks and tangent results. If an ARM64 `libmikktspace.so` already exists, only the probe is run and the library is reused.

## 7. Compressonator (image conversion)

Image compression and conversion use Yellow Dog Man's Compressonator. `stage_compressonator()` fetches the source at a pinned commit (`090a9c12…`), verifies its SHA-256, applies two ARM64 patches, and builds with CMake.

- `cmp_core/shaders/bc1_cmp.h`: on ARM64, pin the SIMD function-pointer setup to the static implementation.
- `applications/_plugins/common/pluginbase.h`: add `<cstdint>`.

Only `CMP_Compressonator` is built; GUI, Qt, KTX2, EXR, and other targets are disabled. The resulting `libCMP_Framework.so` and `libCMP_Compressonator.so` are placed in `runtimes/linux-arm64/native/`.

The build is followed by a small C++ program (`CMP_PROBE`) that compresses and decompresses BC1, BC3, and BC7 and checks the round-trip error. If the check fails, the build log is saved to `~/.local/share/resonite-steam-frame/cache/compressonator-build.log`.

## 8. Video URL playback (yt-dlp + Deno)

`stage_video()` fetches:

- `yt-dlp` `2026.10.26` (`yt-dlp_linux_aarch64`)
- Deno `2.9.8` (`deno` from `deno-aarch64-unknown-linux-gnu.zip`)

Both are verified by SHA-256, checked as ARM64 ELF files, and run with `--version` before being placed in `dotnet-runtime-arm64/tools/`.

`RuntimeData/yt-dlp_linux`, which Resonite invokes, is replaced with an installer-managed shell launcher; the same content is also placed in `.backup` so that Resonite's rollback restores the same launcher. The launcher:

- passes normal arguments through to `tools/yt-dlp_linux_aarch64`;
- does not self-update for `-U` / `--update` arguments and tells the user to update via the installer (`--update-to=nightly` only prints the message and exits successfully);
- adds `tools/` to `PATH` before executing.

This keeps yt-dlp's self-update from replacing the installer-managed ARM64 binary.

## 9. Rewriting Steam settings

The `Settings` class handles two Steam settings.

| Setting | Value |
| --- | --- |
| Launch options | `<helper> %command% -Device SteamVR` from `launch_options()` |
| Compatibility tool | `CompatToolMapping[2519830]` set to `name = proton_experimental`, `config = ""`, `priority = "250"` |

How they are read and written depends on whether Steam is running.

### Steam running (via CDP)

The installer connects over WebSocket to Steam's loopback debug port (`127.0.0.1:8080`) and evaluates JavaScript in `SharedJSContext`:

- read: `appDetailsStore.RequestAppDetails` → `GetAppDetails`
- write: `SteamClient.Apps.SetAppLaunchOptions` → `SteamClient.Apps.SpecifyCompatTool`
- account check: `loginStore.accountName`

`DebugSocket` is a minimal WebSocket implementation for this purpose and restricts connections to `ws://127.0.0.1:8080`. The account is checked before and after writes.

### Steam not running (direct VDF editing)

The installer edits these files directly:

- `config/config.vdf`: `InstallConfigStore > Software > Valve > Steam > CompatToolMapping > 2519830`
- `userdata/<account ID>/config/localconfig.vdf`: `UserLocalConfigStore > Software > Valve > Steam > apps > 2519830`

The account is determined from `LastOwner` (low 32 bits of the SteamID64), `--user-id`, and `loginusers.vdf`. If more than one candidate remains, it fails.

## 10. Installation transactions and restoration

The `Transaction` class manages the manifest (`~/.local/share/resonite-steam-frame/manifest.json`) and backups.

- Status: `applying` → `installed` (`failed` on error, `uninstalled` after restore).
- Records `before` / `after` for each file (kind, SHA-256, permissions).
- Saves the original file under `backups/<timestamp>/`.
- Writes the journal before each write and re-checks for races immediately before writing.
- If the user edited a managed file after installation, the installer stops instead of overwriting it.

VDF entries record only the targeted keys, so restoration returns only those keys and preserves changes made by other apps or to other settings.

If a game update replaces `LinuxBootstrap.sh`, a recognized unpatched script becomes the restore point for the current game version and the update continues.

## Files created and placed

| Path | Contents |
| --- | --- |
| `<game>/dotnet-runtime-arm64/` | ARM64 .NET runtime |
| `<game>/dotnet-runtime-arm64/tools/yt-dlp_linux_aarch64` | yt-dlp (ARM64) |
| `<game>/dotnet-runtime-arm64/tools/deno` | Deno (ARM64) |
| `<game>/frame-libs-arm64/` | ICU, OpenSSL 3, PulseAudio, legacy provider, `openssl.cnf`, and dependencies |
| `<game>/runtimes/linux-arm64/native/libmikktspace.so` | Mikktspace |
| `<game>/runtimes/linux-arm64/native/libCMP_Framework.so` | Compressonator |
| `<game>/runtimes/linux-arm64/native/libCMP_Compressonator.so` | Compressonator |
| `<game>/Renderite.Host.frame.runtimeconfig.json` | Normalized .NET config for Linux |
| `<game>/LinuxBootstrap.sh` | Patched launch script |
| `<game>/RuntimeData/yt-dlp_linux`, `yt-dlp_linux.backup` | Managed yt-dlp launcher |
| `~/.local/bin/frame-resonite-launch` | Launch helper (default) |
| `~/.local/share/resonite-steam-frame/manifest.json` | Installation manifest |
| `~/.local/share/resonite-steam-frame/backups/` | Backups of original files |
| `~/.local/share/resonite-steam-frame/cache/` | Downloads and build logs |
| `~/.local/share/resonite-steam-frame/lib/` | Installer code and locales used by the helper |

`--state-dir` and `--bin-dir` change these locations.

## What `--check` verifies

`check_report()` returns JSON; when run from the menu, the result is shown with whiptail. The main checks are:

- `installation_complete`: the manifest matches the game and Steam roots and status is `installed`
- `managed_files`: no managed file has changed since installation
- `steam_settings_match`: Steam's launch options and compatibility tool match the installed values
- `runtime_arm64` / `runtime_version_available`: an ARM64 .NET runtime satisfying the requirement exists
- `runtime_configuration`: `Renderite.Host.frame.runtimeconfig.json` matches the current game config
- `bootstrap_supported`: `LinuxBootstrap.sh` is patched as expected
- `support_libraries`: ICU, OpenSSL, PulseAudio, legacy provider, and `openssl.cnf` are present
- `mikktspace_arm64` / `compressonator_arm64`: native libraries are ARM64
- `video_launcher` / `video_tools_arm64`: the yt-dlp launcher and Deno / yt-dlp are ARM64
- `changed_files`: files changed after installation

`--check` is read-only and does not modify any files.

## Uninstall

`--uninstall` restores the original state in this order.

1. Confirm Resonite is not running.
2. Confirm the current Steam state (CDP or offline) matches the one recorded at install time.
3. Confirm the current launch options are either the installed or the original value.
4. Restore the Steam settings.
5. Restore files from backup.
6. Set the status to `uninstalled`.

If a file was edited manually after installation, or the launch options became a third value, the installer stops without overwriting.

## Building the installer

`install.sh` is generated from `src/resonite_frame.py` and `src/locales/*.json` and must not be edited by hand.

```bash
python3 tools/build_installer.py
```

The output is a single bash script with the Python source and locales embedded in heredocs. At runtime it extracts them into a temporary directory and runs them with `python3`. With `--check`, it only verifies that `install.sh` is up to date.

## Tests and CI

`.github/workflows/check.yml` runs on push and pull request:

- `python3 -m unittest discover -s tests -v`
- `python3 tools/build_installer.py --check` (detects a stale `install.sh`)
- `bash -n install.sh` (syntax check)

`tests/test_installer.py` covers bootstrap patching, VDF read/write, transactions and restoration, archive safety, runtime selection, locale consistency, account detection, and menu transitions.

## Safety measures

- Linux ARM64 only; refuses to run as `root`.
- Downloads over HTTPS only, verified with SHA-256 / SHA-512.
- Downloads capped at 256 MiB, extraction at 1 GiB; archives containing `..` or absolute paths are rejected.
- Every installed binary must be an ARM64 ELF.
- Mikktspace and Compressonator are exercised through real calls after building.
- No writes while the game is running, Steam is updating, or settings are being changed.
- Changes not recorded in the manifest are never overwritten or restored automatically.

## Limitations

- Requires the Steam version of Resonite and Proton Experimental.
- Requires `c++`, `ldd`, `cmake`, and `make` for builds.
- The Compressonator build takes a few minutes.
- Relies on Steam's internal API (CDP `SharedJSContext`) and the VDF structure, so Steam changes may break it.
- A changed launch script or game layout requires a matching installer release.
