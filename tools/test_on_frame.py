#!/usr/bin/env python3
"""Exercise install/reinstall/uninstall in a temporary Steam tree on ARM64.

The real installation is read only. No game or Steam process is started/stopped.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
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
parser.add_argument("--runtime-version", help="Override the isolated fixture runtime requirement")
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
    runtime_config = json.loads((game/"Renderite.Host.runtimeconfig.json").read_text())
    options = runtime_config["runtimeOptions"]
    frameworks = options.get("frameworks", [options.get("framework", {})])
    required_version = args.runtime_version or next(f["version"] for f in frameworks if f["name"] == "Microsoft.NETCore.App")
    channel = '.'.join(required_version.split('.')[:2])
    if args.runtime_version:
        runtime_config = {"runtimeOptions": {"tfm": "net" + channel, "framework": {"name": "Microsoft.NETCore.App", "version": required_version}}}
    (fixture/"Renderite.Host.runtimeconfig.json").write_text(json.dumps(runtime_config))
    bootstrap = (game/"LinuxBootstrap.sh").read_text()
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
    (steam/"steamapps/appmanifest_2519830.acf").write_text('"AppState" { "appid" "2519830" "installdir" "Resonite" "buildid" "1" "StateFlags" "4" }\n')
    experimental = steam/"steamapps/common/Proton - Experimental"
    experimental.mkdir()
    # compatibilitytool.vdf is included only for testing the old v0.1.0 installer.
    for name in ("proton", "toolmanifest.vdf", "compatibilitytool.vdf"):
        (experimental/name).write_text('"fixture" "only"\n')
    config = steam/"config/config.vdf"
    config.parent.mkdir()
    config.write_text('"InstallConfigStore" { "Software" { "Valve" { "Steam" { "CompatToolMapping" { "2519830" { "name" "proton_11-arm64" "config" "old" "priority" "100" } } "other_setting" "keep me" } } } }\n')
    # Generate a synthetic account directory for this isolated Steam tree.
    account_id = str(secrets.randbelow(2**31 - 1) + 1)
    local = steam/"userdata"/account_id/"config/localconfig.vdf"
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
        # Only legacy installers expose a build-allowlist override.
        options = subprocess.run(prior[:2] + ["--help"], text=True, capture_output=True)
        assert options.returncode == 0, options.stdout + options.stderr
        if "--allow-untested-build" in options.stdout:
            prior.append("--allow-untested-build")
        previous = subprocess.run(prior, text=True, capture_output=True)
        assert previous.returncode == 0, previous.stdout + previous.stderr
        assert json.loads((state/"manifest.json").read_text())["status"] == "installed"
    run = subprocess.run(command + ["--install"], text=True, capture_output=True)
    print(run.stdout, end="")
    if run.returncode:
        raise RuntimeError(run.stderr)
    manifest = json.loads((state/"manifest.json").read_text())
    assert manifest["status"] == "installed"
    assert (fixture/"Renderite.Host.runtimeconfig.json").read_text() == json.dumps(runtime_config)
    native_config = json.loads((fixture/"Renderite.Host.frame.runtimeconfig.json").read_text())
    native_options = native_config["runtimeOptions"]
    native_frameworks = native_options.get("frameworks", [native_options.get("framework", {})])
    assert [f["name"] for f in native_frameworks] == ["Microsoft.NETCore.App"]
    runtime_list = subprocess.run([str(fixture/"dotnet-runtime-arm64/dotnet"), "--list-runtimes"], capture_output=True, text=True)
    assert runtime_list.returncode == 0, runtime_list.stdout + runtime_list.stderr
    assert any(line.split()[:2] == ["Microsoft.NETCore.App", manifest["dotnet_version"]] for line in runtime_list.stdout.splitlines()), runtime_list.stdout
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
    native_version = subprocess.run([str(fixture/"dotnet-runtime-arm64/tools/yt-dlp_linux_aarch64"), "--version"], capture_output=True, text=True)
    assert native_version.returncode == 0 and native_version.stdout.strip(), native_version.stdout + native_version.stderr
    assert version.returncode == 0 and version.stdout.strip() == native_version.stdout.strip(), version.stdout + version.stderr
    assert (fixture/"dotnet-runtime-arm64/tools/deno").is_file()
    assert f'--version {manifest["dotnet_version"]}' in (fixture/"LinuxBootstrap.sh").read_text()
    check = subprocess.run(command + ["--check"], text=True, capture_output=True)
    assert check.returncode == 0, check.stdout + check.stderr
    again = subprocess.run(command + ["--install"], text=True, capture_output=True)
    assert again.returncode == 0, again.stdout + again.stderr
    assert json.loads((state/"manifest.json").read_text()) == manifest
    # Steam may update unrelated settings after installation. Preserve them.
    local.write_text(local.read_text().replace('"before"', '"after"'))
    restored = subprocess.run(command + ["--uninstall"], text=True, capture_output=True)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert (fixture/"LinuxBootstrap.sh").read_text() == bootstrap
    assert not (fixture/"Renderite.Host.frame.runtimeconfig.json").exists()
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
