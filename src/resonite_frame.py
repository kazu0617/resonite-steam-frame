#!/usr/bin/env python3
"""App-local Resonite support for Steam Frame. Standard library only."""
import argparse
import base64
import ctypes
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

VERSION = "0.1.0"
APP_ID = "2519830"
TESTED_BUILD = "26579448"
DOTNET_VERSION = "10.0.15"
MIKK_COMMIT = "a25b708d852d88848d4c729677cd8dbad4526523"
MIKK_URL = f"https://api.github.com/repos/Yellow-Dog-Man/Mikktspace.NET/tarball/{MIKK_COMMIT}"
MIKK_SHA256 = "987e5056e13468ca2375ce8400329af36ec0dc22090f15e42c31f9c74599d250"
OPENSSL_CONF = """openssl_conf = openssl_init
[openssl_init]
providers = provider_sect
[provider_sect]
default = default_sect
legacy = legacy_sect
[default_sect]
activate = 1
[legacy_sect]
activate = 1
"""


class SetupError(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def locked_state(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root/".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("別のインストールまたは復元を実行中です。完了を待ってください。")
        yield
    finally:
        os.close(descriptor)


def atomic(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".resonite-frame-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def elf_arm64(path):
    with Path(path).open("rb") as stream:
        header = stream.read(20)
    return header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == 183


def vdf_load(text):
    """Parse textual KeyValues; reject duplicate keys rather than lose data."""
    tokens = re.findall(r'//[^\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+', text)
    tokens = [t for t in tokens if not t.startswith("//")]
    index = 0

    def value(token):
        if not token.startswith('"'):
            return token
        return re.sub(r'\\(.)', lambda m: {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(m[1], "\\" + m[1]), token[1:-1])

    def block(nested):
        nonlocal index
        result = {}
        while index < len(tokens):
            key = tokens[index]
            index += 1
            if key == "}":
                if not nested:
                    raise SetupError("VDF: unexpected closing brace")
                return result
            if key == "{" or index >= len(tokens):
                raise SetupError("VDF: missing key or value")
            key = value(key)
            if any(k.casefold() == key.casefold() for k in result):
                raise SetupError("VDF: duplicate keys; refusing a lossy rewrite")
            item = tokens[index]
            index += 1
            if item == "}":
                raise SetupError("VDF: missing value")
            result[key] = block(True) if item == "{" else value(item)
        if nested:
            raise SetupError("VDF: missing closing brace")
        return result

    return block(False)


def vdf_dump(data, depth=0):
    def quote(s):
        return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'
    lines = []
    pad = "\t" * depth
    for key, val in data.items():
        if isinstance(val, dict):
            lines.extend([pad + quote(key), pad + "{", vdf_dump(val, depth + 1).rstrip("\n"), pad + "}"])
        else:
            lines.append(pad + quote(key) + "\t\t" + quote(val))
    return "\n".join(lines) + "\n"


def subtree(data, *keys, create=False):
    for key in keys:
        actual = next((k for k in data if k.casefold() == key.casefold()), key)
        if actual not in data:
            if not create:
                return {}
            data[actual] = {}
        data = data[actual]
        if not isinstance(data, dict):
            raise SetupError("Unexpected scalar in Steam configuration")
    return data


def steam_root(explicit=None):
    for path in ([Path(explicit)] if explicit else [Path.home()/".local/share/Steam", Path.home()/".steam/steam"]):
        if (path/"steamapps").is_dir():
            return path.resolve()
    raise SetupError("Steamが見つかりません。--steam-root で指定してください。")


def libraries(steam):
    roots = [steam]
    for path in [steam/"steamapps/libraryfolders.vdf", steam/"config/libraryfolders.vdf"]:
        if path.is_file():
            data = subtree(vdf_load(path.read_text()), "libraryfolders")
            for item in data.values():
                name = item.get("path") if isinstance(item, dict) else item
                if name and Path(name).is_dir() and Path(name).resolve() not in roots:
                    roots.append(Path(name).resolve())
    return roots


def find_game(steam, explicit=None):
    for root in libraries(steam):
        manifest = root/f"steamapps/appmanifest_{APP_ID}.acf"
        if manifest.is_file():
            app = subtree(vdf_load(manifest.read_text()), "AppState")
            game = root/"steamapps/common"/app.get("installdir", "Resonite")
            if explicit and Path(explicit).resolve() != game.resolve():
                continue
            if (game/"Renderite.Host.dll").is_file() and (game/"LinuxBootstrap.sh").is_file():
                return game.resolve(), app
    raise SetupError("Steam版Resoniteを先にインストールしてください。")


def processes():
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            yield path, (path/"cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, ProcessLookupError):
            pass


def game_running(game):
    for proc, command in processes():
        if "Renderite.Host.dll" in command or "Renderite.Renderer.exe" in command:
            try:
                if str(game) in command or (proc/"cwd").resolve() == game:
                    return True
            except OSError:
                pass
    return False


def steam_running(steam):
    for proc, command in processes():
        try:
            name = (proc/"comm").read_text().strip()
            if name in ("steam", "steamwebhelper") and str(steam) in command:
                return True
        except OSError:
            pass
    return False


class DebugSocket:
    """Minimal CDP transport, restricted to Steam's existing loopback port."""
    def __init__(self, url):
        parsed = urlparse(url)
        if (parsed.scheme, parsed.hostname, parsed.port) != ("ws", "127.0.0.1", 8080):
            raise SetupError("Unexpected Steam debugging address")
        self.sock = socket.create_connection(("127.0.0.1", 8080), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {parsed.path} HTTP/1.1\r\nHost: 127.0.0.1:8080\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            header += self.exact(1)
            if len(header) > 16384:
                raise SetupError("Oversized websocket handshake")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if not header.startswith(b"HTTP/1.1 101 ") or expected.lower() not in header.lower():
            raise SetupError("Steam websocket handshake rejected")

    def exact(self, count):
        data = bytearray()
        while len(data) < count:
            part = self.sock.recv(count - len(data))
            if not part:
                raise SetupError("Steam websocket closed")
            data.extend(part)
        return bytes(data)

    def send(self, payload, opcode=1):
        payload = payload.encode() if isinstance(payload, str) else payload
        length = len(payload)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | length]) if length < 126 else bytes([0xfe]) + struct.pack("!H", length) if length < 65536 else bytes([0xff]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

    def receive(self):
        chunks = bytearray()
        while True:
            first, second = self.exact(2)
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self.exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.exact(8))[0]
            if size > 16 * 1024 * 1024:
                raise SetupError("Oversized websocket message")
            mask = self.exact(4) if second & 128 else None
            data = self.exact(size)
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            opcode = first & 15
            if opcode == 8:
                raise SetupError("Steam websocket closed")
            if opcode == 9:
                self.send(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise SetupError("Unexpected websocket message")
            chunks.extend(data)
            if len(chunks) > 16 * 1024 * 1024:
                raise SetupError("Oversized websocket message")
            if first & 128:
                return json.loads(chunks)


def cdp(expression):
    # Do not send GitHub credentials or remote requests to this endpoint.
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:8080/json", timeout=2))
    target = next((t for t in targets if t.get("title") == "SharedJSContext"), None)
    if not target:
        raise SetupError("Steam SharedJSContext is unavailable")
    wire = DebugSocket(target["webSocketDebuggerUrl"])
    try:
        wire.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True, "awaitPromise": True, "timeout": 10000}}))
        for _ in range(100):
            response = wire.receive()
            if response.get("id") != 1:
                continue
            result = response.get("result", {})
            if "error" in response or "exceptionDetails" in result:
                raise SetupError("Steam setting API failed")
            return result.get("result", {}).get("value")
        raise SetupError("Steam setting API did not reply")
    finally:
        wire.sock.close()


def live_settings():
    cdp("(()=>{appDetailsStore.RequestAppDetails(2519830);return true})()")
    for _ in range(10):
        result = cdp("(()=>{let d=appDetailsStore.GetAppDetails(2519830);if(!d||d.strLaunchOptions===undefined||d.strCompatToolName===undefined)return null;return {launch:d.strLaunchOptions,compat:d.strCompatToolName}})()")
        if result is not None:
            return result
        time.sleep(0.2)
    raise SetupError("Steam app details are not loaded")


def local_config(steam, app, user_id=None):
    paths = list(steam.glob("userdata/*/config/localconfig.vdf"))
    owner = str(int(app.get("LastOwner", "0")) - 76561197960265728)
    selected = user_id or owner
    match = next((p for p in paths if p.parents[1].name == selected), None)
    if match:
        return match
    if len(paths) == 1 and not user_id:
        return paths[0]
    raise SetupError("Steamアカウントを特定できません。--user-id にuserdata内の番号を指定してください。")


class Settings:
    def __init__(self, steam, app, user_id=None):
        self.steam = steam
        self.config = steam/"config/config.vdf"
        self.local = None
        if steam_running(steam):
            try:
                self.before = live_settings()
                self.mode = "cdp"
                return
            except (OSError, ValueError, SetupError):
                raise SetupError("Steamが起動中で設定APIを利用できません。Steamを手動で終了してから再実行してください。自動終了はしません。")
        self.mode = "offline"
        self.local = local_config(steam, app, user_id)
        self.before = self.read()

    def read(self):
        if getattr(self, "mode", None) == "cdp":
            result = live_settings()
            if not isinstance(result, dict) or set(result) != {"launch", "compat"}:
                raise SetupError("Steam setting readback failed")
            return result
        global_data = vdf_load(self.config.read_text())
        local_data = vdf_load(self.local.read_text())
        compat = subtree(global_data, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", APP_ID)
        launch = subtree(local_data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", APP_ID)
        return {"launch": launch.get("LaunchOptions", ""), "compat": compat.get("name", "")}

    def apply(self, wanted, transaction=None, expected=None):
        if expected is not None and self.read() != expected:
            raise SetupError("Steam設定が途中で変わりました。上書きを中止しました。")
        if self.mode == "cdp":
            cdp("(async()=>{await SteamClient.Apps.SetAppLaunchOptions(2519830," + json.dumps(wanted["launch"]) + ");await SteamClient.Apps.SpecifyCompatTool(2519830," + json.dumps(wanted["compat"]) + ");return true})()")
            for _ in range(20):
                if self.read() == wanted:
                    return
                time.sleep(0.2)
            raise SetupError("Steam設定の読み戻しが一致しません。バックアップを使って復元してください。")
        if steam_running(self.steam):
            raise SetupError("Steamが起動しました。設定の書き換えを中止しました。")
        for path, keys, changes in [
            (self.config, ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", APP_ID), {"name": wanted["compat"], "config": "", "priority": "250"}),
            (self.local, ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", APP_ID), {"LaunchOptions": wanted["launch"]}),
        ]:
            original = path.read_bytes()
            data = vdf_load(original.decode())
            subtree(data, *keys, create=True).update(changes)
            if path.read_bytes() != original or steam_running(self.steam):
                raise SetupError("Steam設定が更新されました。上書きを中止しました。")
            if transaction:
                transaction.put_vdf(path, keys, changes)
            else:
                atomic(path, vdf_dump(data).encode(), path.stat().st_mode & 0o777)
        if self.read() != wanted:
            raise SetupError("Steam設定の読み戻しが一致しません。")


def launch_options(previous, helper):
    prefix = shlex.quote(str(helper))
    if str(helper) in previous:
        if previous.startswith(prefix + " %command%"):
            return previous if "-Device" in previous else previous + " -Device SteamVR"
        raise SetupError("既存のラッパー設定を自動変換できません。起動オプションを確認してください。")
    if "%command%" in previous:
        if previous.strip() != "%command%":
            raise SetupError("独自の%command%設定があります。バックアップのうえ手動で統合してください。")
        previous = ""
    return prefix + " %command%" + (" " + previous.strip() if previous.strip() else "") + ("" if "-Device" in previous else " -Device SteamVR")


def patch_bootstrap(original):
    pairs = [
        ('DOTNET_ROOT="$PWD/dotnet-runtime"', 'DOTNET_ROOT="$PWD/dotnet-runtime-arm64"'),
        ('--verbose --channel 10.0 --runtime dotnet --install-dir "$DOTNET_ROOT"', f'--verbose --version {DOTNET_VERSION} --runtime dotnet --architecture arm64 --install-dir "$DOTNET_ROOT"'),
    ]
    # Accept the exact manually verified earlier ARM64 patch as an input too.
    original = original.replace('--verbose --channel 10.0 --runtime dotnet --architecture arm64 --install-dir "$DOTNET_ROOT"', pairs[1][1])
    for old, new in pairs:
        if original.count(new) == 1 and old not in original:
            continue
        if original.count(old) != 1 or new in original:
            raise SetupError("LinuxBootstrap.shの形式が変わっています。自動変更を中止しました。")
        original = original.replace(old, new, 1)
    command = '"$DOTNET_EXECUTABLE" "$@"'
    prefix = ('OPENSSL_MODULES="$PWD/frame-libs-arm64/ossl-modules" '
              'OPENSSL_CONF="$PWD/frame-libs-arm64/openssl.cnf" '
              'LD_LIBRARY_PATH="$PWD/frame-libs-arm64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ')
    if original.count(prefix + command) == 1:
        return original
    if original.count(command) != 1:
        raise SetupError("dotnetの起動箇所を特定できません。")
    return original.replace(command, prefix + command, 1)


def snapshot(path):
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.is_file():
        return {"kind": "file", "sha256": digest(path.read_bytes()), "mode": path.stat().st_mode & 0o777}
    if path.exists():
        raise SetupError(f"Expected a file: {path}")
    return {"kind": "absent"}


def vdf_values(path, keys, names):
    node = subtree(vdf_load(path.read_text()), *keys)
    return {name: node.get(next((k for k in node if k.casefold() == name.casefold()), name)) for name in names}


def vdf_edit(original, keys, changes):
    data = vdf_load(original.decode())
    node = subtree(data, *keys, create=True)
    for key, value in changes.items():
        actual = next((k for k in node if k.casefold() == key.casefold()), key)
        if value is None:
            node.pop(actual, None)
        else:
            node[actual] = value
    return vdf_dump(data).encode()


def entry_value(entry):
    path = Path(entry["path"])
    if entry.get("type") == "vdf":
        return vdf_values(path, entry["keys"], entry["after"])
    return snapshot(path)


class Transaction:
    def __init__(self, root, metadata):
        self.root = root
        self.path = root/"manifest.json"
        self.data = {"version": VERSION, "status": "applying", "files": [], **metadata}
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.backups = root/"backups"/str(time.time_ns())
        self.backups.mkdir(parents=True, mode=0o700)
        self.save()

    def save(self):
        atomic(self.path, (json.dumps(self.data, ensure_ascii=False, indent=2) + "\n").encode(), 0o600)

    def put(self, path, data, mode=0o644):
        path = path.absolute()
        before = snapshot(path)
        after = {"kind": "file", "sha256": digest(data), "mode": mode}
        if before == after:
            return
        entry = {"path": str(path), "before": before, "after": after, "applied": False}
        if before["kind"] == "file":
            backup = self.backups/str(len(self.data["files"]))
            atomic(backup, path.read_bytes(), 0o600)
            entry["backup"] = str(backup)
        self.data["files"].append(entry)
        self.save()  # Journal before the write, including a recoverable original.
        if snapshot(path) != before:
            raise SetupError(f"File changed during installation: {path}")
        atomic(path, data, mode)
        entry["applied"] = True
        self.save()

    def put_vdf(self, path, keys, changes):
        original = path.read_bytes()
        before = vdf_values(path, keys, changes)
        if before == changes:
            return
        backup = self.backups/str(len(self.data["files"]))
        atomic(backup, original, 0o600)
        entry = {"type": "vdf", "path": str(path), "keys": keys, "before": before,
                 "after": changes, "backup": str(backup), "backup_sha256": digest(original), "applied": False}
        self.data["files"].append(entry)
        self.save()
        if path.read_bytes() != original:
            raise SetupError("Steam configuration changed during the write")
        atomic(path, vdf_edit(original, keys, changes), path.stat().st_mode & 0o777)
        entry["applied"] = True
        self.save()


def validate_restore(data):
    # Verify every file before restoring any, avoiding a partially forced rollback.
    for entry in data["files"]:
        current = entry_value(entry)
        if current not in (entry["after"], entry["before"]):
            raise SetupError(f"導入後に変更されたファイルがあります。自動復元しません: {entry['path']}")
        expected_backup = entry.get("backup_sha256") if entry.get("type") == "vdf" else entry["before"].get("sha256")
        if expected_backup and digest(Path(entry["backup"]).read_bytes()) != expected_backup:
            raise SetupError("Backup checksum mismatch")


def restore_files(data):
    validate_restore(data)
    for entry in reversed(data["files"]):
        path = Path(entry["path"])
        current = entry_value(entry)
        if current == entry["before"]:
            continue
        if current != entry["after"]:
            raise SetupError(f"File changed during restoration: {path}")
        if entry.get("type") == "vdf":
            if "steam" in data and steam_running(Path(data["steam"])):
                raise SetupError("Steamが起動しました。設定の復元を中止しました。")
            atomic(path, vdf_edit(path.read_bytes(), entry["keys"], entry["before"]), path.stat().st_mode & 0o777)
            continue
        before = entry["before"]
        if before["kind"] == "absent":
            path.unlink()
        elif before["kind"] == "symlink":
            path.unlink()
            path.symlink_to(before["target"])
        else:
            atomic(path, Path(entry["backup"]).read_bytes(), before["mode"])


def download(url, destination, expected=None, algorithm="sha256"):
    if urlparse(url).scheme != "https":
        raise SetupError("Only HTTPS downloads are allowed")
    if destination.is_file() and expected and hashlib.new(algorithm, destination.read_bytes()).hexdigest() == expected.lower():
        return
    request = urllib.request.Request(url, headers={"User-Agent": f"resonite-steam-frame/{VERSION}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(256 * 1024 * 1024 + 1)
    if len(data) > 256 * 1024 * 1024:
        raise SetupError("Download exceeds size limit")
    if expected and hashlib.new(algorithm, data).hexdigest() != expected.lower():
        raise SetupError("ダウンロードのハッシュが一致しません。")
    atomic(destination, data)


def unpack(archive, target, strip=0):
    with tarfile.open(archive) as bundle:
        total = 0
        for member in bundle:
            parts = Path(member.name).parts[strip:]
            if not parts:
                continue
            if member.name.startswith("/") or ".." in Path(member.name).parts or not (member.isfile() or member.isdir()):
                raise SetupError("Unsupported or unsafe archive member")
            total += member.size
            if total > 1024 * 1024 * 1024:
                raise SetupError("Archive exceeds extraction size limit")
            destination = target.joinpath(*parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                atomic(destination, bundle.extractfile(member).read(), 0o755 if member.mode & 0o111 else 0o644)


def dotnet_download_file(release):
    return next(f for f in release["runtime"]["files"] if f["rid"] == "linux-arm64" and f["name"] == "dotnet-runtime-linux-arm64.tar.gz")


def stage_runtime(game, stage, cache):
    runtime = game/"dotnet-runtime-arm64"
    if (runtime/f"shared/Microsoft.NETCore.App/{DOTNET_VERSION}/libcoreclr.so").is_file() and elf_arm64(runtime/"dotnet"):
        return None
    metadata = cache/"dotnet-releases.json"
    download("https://builds.dotnet.microsoft.com/dotnet/release-metadata/10.0/releases.json", metadata)
    release = next((r for r in json.loads(metadata.read_text())["releases"] if r.get("runtime", {}).get("version") == DOTNET_VERSION), None)
    if not release:
        raise SetupError(f".NET {DOTNET_VERSION}の配布情報が見つかりません。")
    item = dotnet_download_file(release)
    host = urlparse(item["url"]).hostname or ""
    if not (host.endswith(".microsoft.com") or host.endswith(".visualstudio.microsoft.com") or host == "dotnetcli.azureedge.net"):
        raise SetupError("Unexpected .NET download host")
    archive = cache/f"dotnet-runtime-{DOTNET_VERSION}-linux-arm64.tar.gz"
    download(item["url"], archive, item["hash"], "sha512")
    unpack(archive, stage)
    if not elf_arm64(stage/"dotnet"):
        raise SetupError("Downloaded .NET host is not ARM64")
    return stage


CORE_LIBS = ("libc.so", "libm.so", "libdl.so", "librt.so", "libpthread.so", "libgcc_s.so", "libstdc++.so", "libresolv.so", "ld-linux")


def native_dependencies(output):
    for line in output.splitlines():
        match = re.match(r"\s*(\S+)\s+=>\s+(/\S+)", line)
        # ldd can print the loader itself with an absolute path on the left.
        # It must never become an absolute destination in the staging tree.
        if match and match[1] == Path(match[1]).name and not match[1].startswith(CORE_LIBS):
            yield Path(match[2]), match[1]


def stage_host_libraries(stage):
    roots = [Path("/usr/lib"), Path("/usr/lib/aarch64-linux-gnu")]
    def locate(pattern):
        found = sorted(p for root in roots for p in root.glob(pattern) if p.is_file())
        if not found:
            raise SetupError(f"ホスト側のARM64ライブラリがありません: {pattern}")
        return found[-1]
    queue = [(locate(n), n) for n in ("libicuuc.so", "libicui18n.so", "libicudata.so", "libssl.so.3", "libcrypto.so.3", "libpulse.so.0")]
    queue += [(locate("pulseaudio/libpulsecommon-*.so"), None), (locate("ossl-modules/legacy.so"), "ossl-modules/legacy.so")]
    visited = set()
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    while queue:
        source, alias = queue.pop()
        real = source.resolve()
        if not elf_arm64(real):
            raise SetupError(f"Host library is not ARM64: {source}")
        name = alias or source.name
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise SetupError("Unsafe library alias")
        atomic(stage/name, real.read_bytes())
        if real in visited:
            continue
        visited.add(real)
        if "/" not in name:
            atomic(stage/real.name, real.read_bytes())
            for sibling in source.parent.glob(source.name.split(".so")[0] + ".so*"):
                if sibling.resolve() == real:
                    atomic(stage/sibling.name, real.read_bytes())
        result = subprocess.run(["ldd", str(real)], env=env, text=True, capture_output=True, check=True)
        if "not found" in result.stdout:
            raise SetupError(f"Unresolved native dependency: {source}")
        queue.extend(native_dependencies(result.stdout))
    atomic(stage/"openssl.cnf", OPENSSL_CONF.encode())


def probe_mikk(library):
    # Exercise the exported function and every callback on a known UV triangle.
    pointer = ctypes.c_void_p
    scalar = ctypes.c_float
    output = ctypes.POINTER(scalar)
    get_count = ctypes.CFUNCTYPE(ctypes.c_int, pointer)
    get_vertices = ctypes.CFUNCTYPE(ctypes.c_int, pointer, ctypes.c_int)
    get_data = ctypes.CFUNCTYPE(None, pointer, output, ctypes.c_int, ctypes.c_int)
    set_basic = ctypes.CFUNCTYPE(None, pointer, output, scalar, ctypes.c_int, ctypes.c_int)
    positions = [(0., 0., 0.), (1., 0., 0.), (0., 1., 0.)]
    results = []
    callbacks = [get_count(lambda c: 1), get_vertices(lambda c, f: 3),
        get_data(lambda c, p, f, v: [p.__setitem__(i, value) for i, value in enumerate(positions[v])]),
        get_data(lambda c, p, f, v: [p.__setitem__(i, value) for i, value in enumerate((0., 0., 1.))]),
        get_data(lambda c, p, f, v: [p.__setitem__(i, value) for i, value in enumerate(positions[v][:2])]),
        set_basic(lambda c, p, sign, f, v: results.append((p[0], p[1], p[2], sign)))]
    interface = (pointer * 7)(*(ctypes.cast(c, pointer).value for c in callbacks), None)
    context = (pointer * 2)(ctypes.addressof(interface), None)
    native = ctypes.CDLL(str(library))
    native.genMikktTangSpace.argtypes = [pointer, scalar]
    native.genMikktTangSpace.restype = ctypes.c_int
    ok = native.genMikktTangSpace(ctypes.byref(context), 180.)
    if not ok or len(results) != 3 or any(any(abs(a-b) > 0.00001 for a, b in zip(row, (1., 0., 0., 1.))) for row in results):
        raise SetupError("mikktspaceの計算テストに失敗しました。")


def stage_mikk(game, stage, cache):
    existing = game/"runtimes/linux-arm64/native/libmikktspace.so"
    if existing.is_file() and elf_arm64(existing):
        probe_mikk(existing)
        return None
    archive = cache/f"mikktspace-{MIKK_COMMIT}.tar.gz"
    download(MIKK_URL, archive, MIKK_SHA256)
    source = stage/"source"
    unpack(archive, source, strip=1)
    destination = stage/"libmikktspace.so"
    subprocess.run(["c++", "-std=c++11", "-O3", "-DNDEBUG", "-fPIC", "-shared", str(source/"Native/src/mikktspace.c"), str(source/"Native/src/mikktspace_api.cpp"), "-Wl,--no-undefined", "-o", str(destination)], check=True)
    if not elf_arm64(destination):
        raise SetupError("Built mikktspace is not ARM64")
    probe_mikk(destination)
    return destination


def helper_source(state_root):
    return ("#!/usr/bin/env python3\nimport sys\n" + f"sys.path.insert(0, {str(state_root/'lib')!r})\n" + "from resonite_frame import launch\n" + f"launch({str(state_root)!r})\n").encode()


def launch(state_root):
    try:
        state = json.loads((Path(state_root)/"manifest.json").read_text())
        if state["status"] != "installed":
            raise SetupError("セットアップが完了していません。インストーラーの状態を確認してください。")
        game = Path(state["game"])
        manifest = subtree(vdf_load((game.parents[1]/f"appmanifest_{APP_ID}.acf").read_text()), "AppState")
        if manifest.get("buildid") != state["build"]:
            raise SetupError("Resoniteが更新されました。新しいbuildを確認してから再導入してください。")
        for relative in state["required"]:
            if not (game/relative).is_file():
                raise SetupError(f"Missing support file: {relative}")
        script = game/"LinuxBootstrap.sh"
        text = script.read_text()
        patched = patch_bootstrap(text)
        if patched != text:
            # A changed bootstrap needs installer review, retaining the original journal.
            raise SetupError("起動スクリプトが更新されています。インストーラーを再確認してください。")
        if len(sys.argv) < 2:
            raise SetupError("Missing Steam launch command")
        os.execvpe(sys.argv[1], sys.argv[1:], dict(os.environ, PROTON_SET_GAME_DRIVE="0", FEX_TSOENABLED="1"))
    except (OSError, ValueError, SetupError) as error:
        print(f"Resonite Frame: {error}", file=sys.stderr)
        raise SystemExit(1)


def check(game, app, state_root):
    result = {"game": str(game), "build": app.get("buildid"), "tested_build": app.get("buildid") == TESTED_BUILD,
              "game_running": game_running(game), "installer_manifest": (state_root/"manifest.json").is_file(),
              "runtime_arm64": (game/"dotnet-runtime-arm64/dotnet").is_file() and elf_arm64(game/"dotnet-runtime-arm64/dotnet"),
              "runtime_version_available": (game/f"dotnet-runtime-arm64/shared/Microsoft.NETCore.App/{DOTNET_VERSION}/libcoreclr.so").is_file(),
              "support_libraries": {name: (game/"frame-libs-arm64"/name).is_file() for name in ["libicuuc.so", "libssl.so.3", "libcrypto.so.3", "libpulse.so.0", "ossl-modules/legacy.so", "openssl.cnf"]},
              "mikktspace_arm64": (game/"runtimes/linux-arm64/native/libmikktspace.so").is_file() and elf_arm64(game/"runtimes/linux-arm64/native/libmikktspace.so")}
    try:
        patch_bootstrap((game/"LinuxBootstrap.sh").read_text())
        result["bootstrap_supported"] = True
    except SetupError:
        result["bootstrap_supported"] = False
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # This is a readiness report, not a live VR/gameplay test.
    return 0 if result["tested_build"] and result["runtime_arm64"] and result["runtime_version_available"] and all(result["support_libraries"].values()) and result["mikktspace_arm64"] and result["bootstrap_supported"] else 1


def uninstall(state_root):
    path = state_root/"manifest.json"
    data = json.loads(path.read_text())
    if data["status"] == "uninstalled":
        print("すでに復元済みです。")
        return
    game, steam = Path(data["game"]), Path(data["steam"])
    if game_running(game):
        raise SetupError("Resoniteを手動で終了してから復元してください。自動終了はしません。")
    settings = Settings(steam, data["app"], data.get("user_id"))
    if data["settings_mode"] == "cdp":
        if settings.mode != "cdp":
            raise SetupError("導入時は起動中SteamのAPIを使いました。Steamを手動で起動し、APIを利用できる状態で復元してください。")
        current = settings.read()
        if any(current[k] not in (data["settings_after"][k], data["settings_before"][k]) for k in current):
            raise SetupError("起動オプションが導入後に変更されています。自動復元しません。")
        # Validate files before changing even the live settings.
        validate_restore(data)
        if current != data["settings_before"]:
            settings.apply(data["settings_before"], expected=current)
    elif settings.mode != "offline":
        raise SetupError("導入時はSteamを終了していました。復元時もSteamを手動で終了してください。")
    restore_files(data)
    data["status"] = "uninstalled"
    atomic(path, (json.dumps(data, ensure_ascii=False, indent=2)+"\n").encode(), 0o600)
    print("導入前のファイルとSteam設定へ復元しました。バックアップと取得済みキャッシュは残しています。")


def install(args, steam, game, app, state_root):
    if game_running(game):
        raise SetupError("Resoniteが起動中です。手動で終了してから実行してください。自動終了・再起動はしません。--check は起動中でも使えます。")
    if app.get("StateFlags") != "4":
        raise SetupError("Resoniteのインストール・更新が完了していません。Steamで完了を待ってください。")
    if app.get("buildid") != TESTED_BUILD and not args.allow_untested_build:
        raise SetupError(f"未検証のResonite build {app.get('buildid')} です。対応状況を確認してください。検証を承知して進める場合は --allow-untested-build を指定します。")
    runtime_config = json.loads((game/"Renderite.Host.runtimeconfig.json").read_text())
    if runtime_config["runtimeOptions"].get("tfm") != "net10.0":
        raise SetupError("この版は.NET 10を要求するResoniteだけに対応しています。")
    options = runtime_config["runtimeOptions"]
    frameworks = options.get("frameworks", [options.get("framework", {})])
    if len(frameworks) != 1 or frameworks[0].get("name") != "Microsoft.NETCore.App":
        raise SetupError("未対応の.NET framework構成です。")
    required = tuple(int(n) for n in frameworks[0]["version"].split("."))
    if required[:2] != (10, 0) or required > tuple(int(n) for n in DOTNET_VERSION.split(".")):
        raise SetupError("このゲームは固定した.NET runtimeより新しい版を要求しています。")
    for command in ("c++", "ldd"):
        if not shutil.which(command):
            raise SetupError(f"{command} が必要です。システムパッケージは自動変更しません。")
    if not any((root/"steamapps/common/Proton - Experimental/compatibilitytool.vdf").is_file() for root in libraries(steam)):
        raise SetupError("SteamのライブラリからProton Experimentalを先にインストールしてください。")
    script = game/"LinuxBootstrap.sh"
    original = script.read_bytes()
    patched = patch_bootstrap(original.decode()).encode()
    settings = Settings(steam, app, args.user_id)
    helper = (Path(args.bin_dir).expanduser().absolute() if args.bin_dir else Path.home()/".local/bin")/"frame-resonite-launch"
    wanted = {"launch": launch_options(settings.before["launch"], helper), "compat": "proton_experimental"}
    manifest = state_root/"manifest.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text())
        if data["status"] == "installed" and data["game"] == str(game):
            if all(entry_value(e) == e["after"] for e in data["files"]) and settings.read() == data["settings_after"]:
                print("すでに導入済みです。変更はありません。")
                return
            raise SetupError("導入後にファイルか設定が変わっています。バックアップを確認してから復元・再導入してください。")
        if data["status"] != "uninstalled":
            raise SetupError("前回の導入が途中で止まっています。--uninstall で復元してから再実行してください。")
        archived = state_root/f"manifest-uninstalled-{time.time_ns()}.json"
        shutil.copy2(manifest, archived)
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = state_root/"cache"
    cache.mkdir(exist_ok=True, mode=0o700)
    print("必要なファイルを準備しています。ゲーム本体への適用は検証後に行います。", flush=True)
    with tempfile.TemporaryDirectory(prefix="resonite-frame-") as temporary:
        stage = Path(temporary)
        support = stage/"support"
        support.mkdir()
        stage_host_libraries(support)
        runtime = stage_runtime(game, stage/"runtime", cache)
        mikk = stage_mikk(game, stage/"mikk", cache)
        _, latest_app = find_game(steam, str(game))
        if game_running(game) or script.read_bytes() != original or settings.read() != settings.before or latest_app.get("buildid") != app.get("buildid") or latest_app.get("StateFlags") != "4":
            raise SetupError("準備中にゲームまたは設定の状態が変わりました。適用を中止しました。")
        tx = Transaction(state_root, {"game": str(game), "steam": str(steam), "build": app.get("buildid"), "app": {"LastOwner": app.get("LastOwner", "0")}, "user_id": args.user_id,
                  "settings_mode": settings.mode, "settings_before": settings.before, "settings_after": wanted, "required": [], "source_commit": MIKK_COMMIT, "source_sha256": MIKK_SHA256, "dotnet_version": DOTNET_VERSION})
        try:
            for source in sorted(support.rglob("*")):
                if source.is_file():
                    relative = Path("frame-libs-arm64")/source.relative_to(support)
                    tx.put(game/relative, source.read_bytes())
                    tx.data["required"].append(str(relative))
            if runtime:
                for source in sorted(runtime.rglob("*")):
                    if source.is_file():
                        tx.put(game/"dotnet-runtime-arm64"/source.relative_to(runtime), source.read_bytes(), source.stat().st_mode & 0o777)
            if mikk:
                tx.put(game/"runtimes/linux-arm64/native/libmikktspace.so", mikk.read_bytes())
            tx.data["required"] += ["dotnet-runtime-arm64/dotnet", "runtimes/linux-arm64/native/libmikktspace.so"]
            tx.put(script, patched, script.stat().st_mode & 0o777)
            tx.put(state_root/"lib/resonite_frame.py", Path(__file__).read_bytes())
            tx.put(helper, helper_source(state_root), 0o755)
            tx.save()
            if game_running(game):
                raise SetupError("Resoniteが起動しました。Steam設定の適用を中止しました。")
            settings.apply(wanted, tx, expected=settings.before)
            tx.data["status"] = "installed"
            tx.save()
        except BaseException:
            tx.data["status"] = "failed"
            tx.save()
            # Keep the journal, including partially applied Steam API changes.
            print(f"適用が途中で止まりました。復元記録: {manifest}\n--uninstall で今回の変更を復元できます。", file=sys.stderr)
            raise
    print("導入が完了しました。SteamからResoniteを起動してください。自動起動・再起動は行っていません。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Steam Frame向けResonite起動・音声・FBXインポート対応")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="変更せず準備状態を表示")
    modes.add_argument("--uninstall", action="store_true", help="記録した変更を復元")
    parser.add_argument("--steam-root")
    parser.add_argument("--game-dir")
    parser.add_argument("--state-dir", help="バックアップと導入記録の保存先")
    parser.add_argument("--bin-dir", help="起動ヘルパーの保存先。既定は ~/.local/bin")
    parser.add_argument("--user-id")
    parser.add_argument("--allow-untested-build", action="store_true")
    args = parser.parse_args(argv)
    state_root = Path(args.state_dir).expanduser().absolute() if args.state_dir else Path(os.environ.get("XDG_DATA_HOME", str(Path.home()/".local/share")))/"resonite-steam-frame"
    try:
        if platform.system() != "Linux" or platform.machine().lower() not in ("aarch64", "arm64"):
            raise SetupError("このインストーラーはLinux ARM64のSteam Frame用です。")
        if os.geteuid() == 0:
            raise SetupError("sudoは使わず、Steamを使うユーザーで実行してください。")
        if args.uninstall:
            if not (state_root/"manifest.json").is_file():
                raise SetupError("導入記録が見つかりません。--state-dir を確認してください。")
            with locked_state(state_root):
                uninstall(state_root)
            return 0
        steam = steam_root(args.steam_root)
        game, app = find_game(steam, args.game_dir)
        if args.check:
            return check(game, app, state_root)
        if game_running(game):
            raise SetupError("Resoniteを手動で終了してから実行してください。--check は起動中でも使えます。")
        with locked_state(state_root):
            install(args, steam, game, app, state_root)
        return 0
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError, SetupError) as error:
        print(f"Resonite Frame: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
