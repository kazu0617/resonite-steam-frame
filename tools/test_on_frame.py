#!/usr/bin/env python3
"""Exercise install/reinstall/uninstall in a temporary Steam tree on ARM64.

The real installation is read only. No game or Steam process is started/stopped.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--installer", type=Path, required=True)
parser.add_argument("--game", type=Path, required=True, help="Existing verified game; read only")
parser.add_argument("--source-cache", type=Path, help="Optional verified mikktspace source archive")
parser.add_argument("--media-cache", type=Path, help="Optional directory of checksum-verified media downloads")
parser.add_argument("--previous-installer", type=Path, help="Install an older version first to exercise upgrading")
parser.add_argument("--fresh-runtime", action="store_true", help="Download .NET instead of reusing the existing runtime")
args = parser.parse_args()
installer = args.installer.resolve()
game = args.game.resolve()
baseline = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in [game/"LinuxBootstrap.sh", Path.home()/".local/bin/frame-resonite-launch"] if p.is_file()}

def clone_file(source, destination):
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)

with tempfile.TemporaryDirectory(prefix="resonite-installer-integration-") as temporary:
    root = Path(temporary)/"path with spaces"
    steam = root/"Steam"
    fixture = steam/"steamapps/common/Resonite"
    fixture.mkdir(parents=True)
    (fixture/"Renderite.Host.dll").write_bytes(b"fixture only, never executed")
    (fixture/"Renderite.Host.runtimeconfig.json").write_text(json.dumps({"runtimeOptions": {"tfm": "net10.0", "frameworks": [{"name": "Microsoft.NETCore.App", "version": "10.0.0"}]}}))
    bootstrap = '#!/bin/sh\nDOTNET_ROOT="$PWD/dotnet-runtime"\ndotnet() { "$DOTNET_EXECUTABLE" "$@"; }\nbash "$DOTNET_INSTALL_SCRIPT" --verbose --channel 10.0 --runtime dotnet --install-dir "$DOTNET_ROOT"\n'
    (fixture/"LinuxBootstrap.sh").write_text(bootstrap)
    (fixture/"LinuxBootstrap.sh").chmod(0o755)
    media_originals = {
        "RuntimeData/yt-dlp_linux": b"fixture original video tool\n",
        "RuntimeData/yt-dlp_linux.backup": b"fixture original video backup\n",
        "runtimes/linux-arm64/native/libCMP_Framework.so": b"fixture original framework\n",
        "runtimes/linux-arm64/native/libCMP_Compressonator.so": b"fixture original compressonator\n",
    }
    for relative, data in media_originals.items():
        path = fixture/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if not args.fresh_runtime:
        shutil.copytree(game/"dotnet-runtime-arm64", fixture/"dotnet-runtime-arm64", copy_function=clone_file)
    (steam/"steamapps/appmanifest_2519830.acf").write_text('"AppState" { "appid" "2519830" "installdir" "Resonite" "buildid" "26579448" "StateFlags" "4" "LastOwner" "76561197960265851" }\n')
    experimental = steam/"steamapps/common/Proton - Experimental"
    experimental.mkdir()
    # compatibilitytool.vdf is included only for testing the old v0.1.0 installer.
    for name in ("proton", "toolmanifest.vdf", "compatibilitytool.vdf"):
        (experimental/name).write_text('"fixture" "only"\n')
    config = steam/"config/config.vdf"
    config.parent.mkdir()
    config.write_text('"InstallConfigStore" { "Software" { "Valve" { "Steam" { "CompatToolMapping" { "2519830" { "name" "proton_11-arm64" "config" "old" "priority" "100" } } "other_setting" "keep me" } } } }\n')
    local = steam/"userdata/123/config/localconfig.vdf"
    local.parent.mkdir(parents=True)
    local.write_text('"UserLocalConfigStore" { "Software" { "Valve" { "Steam" { "apps" { "2519830" { "LaunchOptions" "-Verbose" } "438100" { "setting" "before" } } } } } }\n')
    state = root/"state"
    if args.source_cache:
        cache = state/"cache"
        cache.mkdir(parents=True)
        shutil.copy2(args.source_cache, cache/"mikktspace-a25b708d852d88848d4c729677cd8dbad4526523.tar.gz")
    if args.media_cache:
        cache = state/"cache"
        cache.mkdir(parents=True, exist_ok=True)
        for source in args.media_cache.iterdir():
            if source.is_file():
                shutil.copy2(source, cache/source.name)
    binary = root/"bin"
    binary.mkdir()
    helper = binary/"frame-resonite-launch"
    helper.write_bytes(b"original fixture helper\n")
    command = ["bash", str(installer), "--steam-root", str(steam), "--bin-dir", str(binary), "--state-dir", str(state)]
    if args.previous_installer:
        prior = [command[0], str(args.previous_installer.resolve()), *command[2:]]
        previous = subprocess.run(prior, text=True, capture_output=True)
        assert previous.returncode == 0, previous.stdout + previous.stderr
        assert json.loads((state/"manifest.json").read_text())["version"] == "0.1.0"
    run = subprocess.run(command, text=True, capture_output=True)
    print(run.stdout, end="")
    if run.returncode:
        raise RuntimeError(run.stderr)
    manifest = json.loads((state/"manifest.json").read_text())
    assert manifest["status"] == "installed"
    assert manifest["settings_mode"] == "offline"
    assert "-Verbose" in manifest["settings_after"]["launch"]
    assert (fixture/"runtimes/linux-arm64/native/libmikktspace.so").is_file()
    tool = fixture/"RuntimeData/yt-dlp_linux"
    initial_tool = tool.read_bytes()
    updated = subprocess.run([str(tool), "-U", "--update-to", "nightly"], capture_output=True, text=True)
    assert updated.returncode == 0, updated.stderr
    # Reproduce Resonite's backup/restore for tools smaller than 1 MB.
    shutil.copy2(tool, tool.with_name("yt-dlp_linux.backup"))
    if tool.stat().st_size < 1_000_000:
        shutil.copy2(tool.with_name("yt-dlp_linux.backup"), tool)
    assert tool.read_bytes() == initial_tool
    version = subprocess.run([str(tool), "--version"], capture_output=True, text=True)
    assert version.returncode == 0 and version.stdout.strip() == "2026.10.26", version.stdout + version.stderr
    assert (fixture/"dotnet-runtime-arm64/tools/deno").is_file()
    assert "--version 10.0.15" in (fixture/"LinuxBootstrap.sh").read_text()
    check = subprocess.run(command + ["--check"], text=True, capture_output=True)
    assert check.returncode == 0, check.stdout + check.stderr
    again = subprocess.run(command, text=True, capture_output=True)
    assert again.returncode == 0 and "すでに導入済み" in again.stdout, again.stdout + again.stderr
    # Steam may update unrelated settings after installation. Preserve them.
    local.write_text(local.read_text().replace('"before"', '"after"'))
    restored = subprocess.run(command + ["--uninstall"], text=True, capture_output=True)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert (fixture/"LinuxBootstrap.sh").read_text() == bootstrap
    assert helper.read_bytes() == b"original fixture helper\n"
    assert not (fixture/"runtimes/linux-arm64/native/libmikktspace.so").exists()
    for relative, data in media_originals.items():
        assert (fixture/relative).read_bytes() == data, relative
    if args.fresh_runtime:
        assert not (fixture/"dotnet-runtime-arm64/dotnet").exists()
    assert '"-Verbose"' in local.read_text()
    assert '"after"' in local.read_text()
    assert '"proton_11-arm64"' in config.read_text()
    assert '"100"' in config.read_text()
    print(json.dumps({"integration": "passed", "apply": True, "idempotent_reinstall": True, "check": True, "uninstall": True, "other_app_setting_preserved": True, "paths_with_spaces": True, "video_update_rollback": True, "upgrade": bool(args.previous_installer)}))

for path, expected in baseline.items():
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, f"Real installation changed: {path}"
print("Real bootstrap and launcher were unchanged.")
