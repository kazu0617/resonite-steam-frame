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
import zipfile
from urllib.parse import urlparse

VERSION = "0.3.0"
APP_ID = "2519830"
MIKK_COMMIT = "a25b708d852d88848d4c729677cd8dbad4526523"
MIKK_URL = f"https://api.github.com/repos/Yellow-Dog-Man/Mikktspace.NET/tarball/{MIKK_COMMIT}"
MIKK_SHA256 = "987e5056e13468ca2375ce8400329af36ec0dc22090f15e42c31f9c74599d250"
CMP_COMMIT = "090a9c123a7100c1f1283fb3a7fbb8aa1c82b039"
CMP_URL = f"https://api.github.com/repos/Yellow-Dog-Man/compressonator/tarball/{CMP_COMMIT}"
CMP_SHA256 = "bfd9d475b8576950986265e09f3569f5e39ebc65ed488ba17ac1c40a062ca54d"
CMP_LIBS = ("libCMP_Framework.so", "libCMP_Compressonator.so")
YTDLP_VERSION = "2026.10.26"
YTDLP_URL = f"https://github.com/yt-dlp/yt-dlp/releases/download/{YTDLP_VERSION}/yt-dlp_linux_aarch64"
YTDLP_SHA256 = "e87e9533e89fe5889abaa21f5114dfe7756d6aa6219b83551add04d48b18e483"
DENO_VERSION = "2.9.8"
DENO_URL = f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/deno-aarch64-unknown-linux-gnu.zip"
DENO_SHA256 = "d49596c20b9788cf8f5d8ddf644103d4bde56ed239753f0bdb92ba9361d51905"
DENO_BINARY_SHA256 = "b484f780e5149ca05c673f1f79f7fb466f12a2295933bdad917af4a8a5c020e8"
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


# Translation files stay beside this module in source, bundles and installed helpers.
LOCALES = Path(__file__).resolve().parent/"locales"
LANGUAGE = "en"
MESSAGES = {}


def load_language(language):
    global LANGUAGE, MESSAGES
    language = {"japanese": "ja", "english": "en"}.get(str(language).casefold(), str(language).casefold())
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", language) or not (LOCALES/f"{language}.json").is_file():
        language = "en"
    fallback = json.loads((LOCALES/"en.json").read_text())
    MESSAGES = {**fallback, **json.loads((LOCALES/f"{language}.json").read_text())}
    LANGUAGE = language
    return language


def tr(key, **values):
    if not MESSAGES:
        load_language("en")
    return MESSAGES[key].format(**values)


def steam_language(explicit=None):
    try:
        steam = steam_root(explicit)
        if steam_running(steam):
            language = cdp("SteamClient.Settings.GetCurrentLanguage()")
            if isinstance(language, str) and language:
                return language
    except (OSError, ValueError, SetupError):
        steam = Path(explicit) if explicit else Path.home()/".local/share/Steam"
    # Steam's Linux registry also retains the client language while it is closed.
    registry = Path.home()/".steam/registry.vdf"
    for path, keys in [
        (registry, ("Registry", "HKCU", "Software", "Valve", "Steam")),
        (steam/"config/config.vdf", ("InstallConfigStore", "Software", "Valve", "Steam")),
    ]:
        try:
            node = subtree(vdf_load(path.read_text()), *keys)
            language = next((v for k, v in node.items() if k.casefold() == "language"), None)
            if isinstance(language, str) and language:
                return language
        except (OSError, ValueError, SetupError):
            continue
    return "english"


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
            raise SetupError(tr('state_busy'))
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
                    raise SetupError(tr('vdf_closing_brace'))
                return result
            if key == "{" or index >= len(tokens):
                raise SetupError(tr('vdf_missing_item'))
            key = value(key)
            if any(k.casefold() == key.casefold() for k in result):
                raise SetupError(tr('vdf_duplicate'))
            item = tokens[index]
            index += 1
            if item == "}":
                raise SetupError(tr('vdf_missing_value'))
            result[key] = block(True) if item == "{" else value(item)
        if nested:
            raise SetupError(tr('vdf_truncated'))
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
            raise SetupError(tr('vdf_structure'))
    return data


def steam_root(explicit=None):
    for path in ([Path(explicit)] if explicit else [Path.home()/".local/share/Steam", Path.home()/".steam/steam"]):
        if (path/"steamapps").is_dir():
            return path.resolve()
    raise SetupError(tr('steam_missing'))


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
    raise SetupError(tr('game_missing'))


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
            raise SetupError(tr('steam_endpoint_invalid'))
        self.sock = socket.create_connection(("127.0.0.1", 8080), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {parsed.path} HTTP/1.1\r\nHost: 127.0.0.1:8080\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            header += self.exact(1)
            if len(header) > 16384:
                raise SetupError(tr('steam_handshake_large'))
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if not header.startswith(b"HTTP/1.1 101 ") or expected.lower() not in header.lower():
            raise SetupError(tr('steam_connect_failed'))

    def exact(self, count):
        data = bytearray()
        while len(data) < count:
            part = self.sock.recv(count - len(data))
            if not part:
                raise SetupError(tr('steam_disconnected'))
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
                raise SetupError(tr('steam_response_large'))
            mask = self.exact(4) if second & 128 else None
            data = self.exact(size)
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            opcode = first & 15
            if opcode == 8:
                raise SetupError(tr('steam_disconnected'))
            if opcode == 9:
                self.send(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise SetupError(tr('steam_response_invalid'))
            chunks.extend(data)
            if len(chunks) > 16 * 1024 * 1024:
                raise SetupError(tr('steam_response_large'))
            if first & 128:
                return json.loads(chunks)


def cdp(expression):
    # Do not send GitHub credentials or remote requests to this endpoint.
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:8080/json", timeout=2))
    target = next((t for t in targets if t.get("title") == "SharedJSContext"), None)
    if not target:
        raise SetupError(tr('steam_endpoint_missing'))
    wire = DebugSocket(target["webSocketDebuggerUrl"])
    try:
        wire.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True, "awaitPromise": True, "timeout": 10000}}))
        for _ in range(100):
            response = wire.receive()
            if response.get("id") != 1:
                continue
            result = response.get("result", {})
            if "error" in response or "exceptionDetails" in result:
                raise SetupError(tr('steam_api_failed'))
            return result.get("result", {}).get("value")
        raise SetupError(tr('steam_no_response'))
    finally:
        wire.sock.close()


def live_settings():
    cdp("(()=>{appDetailsStore.RequestAppDetails(2519830);return true})()")
    for _ in range(10):
        result = cdp("(()=>{let d=appDetailsStore.GetAppDetails(2519830);if(!d||d.strLaunchOptions===undefined||d.strCompatToolName===undefined)return null;return {launch:d.strLaunchOptions,compat:d.strCompatToolName}})()")
        if result is not None:
            return result
        time.sleep(0.2)
    raise SetupError(tr('steam_app_unavailable'))


def local_config(steam, app, user_id=None):
    paths = sorted(p for p in steam.glob("userdata/*/config/localconfig.vdf") if p.parents[1].name.isdecimal())
    selected = user_id
    if selected is None:
        try:
            steam_id64 = int(app.get("LastOwner", "0"))
        except (TypeError, ValueError):
            steam_id64 = 0
        # Steam's userdata directory uses the account ID in SteamID64's low 32 bits.
        selected = str(steam_id64 & 0xFFFFFFFF) if steam_id64 > 0 else None
    match = next((p for p in paths if p.parents[1].name == selected), None)
    if match:
        return match
    if len(paths) == 1 and not user_id:
        return paths[0]
    raise SetupError(tr('steam_account_ambiguous'))


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
                raise SetupError(tr('steam_live_unavailable'))
        self.mode = "offline"
        self.local = local_config(steam, app, user_id)
        self.before = self.read()

    def read(self):
        if getattr(self, "mode", None) == "cdp":
            result = live_settings()
            if not isinstance(result, dict) or set(result) != {"launch", "compat"}:
                raise SetupError(tr('steam_settings_unavailable'))
            return result
        global_data = vdf_load(self.config.read_text())
        local_data = vdf_load(self.local.read_text())
        compat = subtree(global_data, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", APP_ID)
        launch = subtree(local_data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", APP_ID)
        return {"launch": launch.get("LaunchOptions", ""), "compat": compat.get("name", "")}

    def apply(self, wanted, transaction=None, expected=None):
        if expected is not None and self.read() != expected:
            raise SetupError(tr('steam_settings_changed'))
        if self.mode == "cdp":
            cdp("(async()=>{await SteamClient.Apps.SetAppLaunchOptions(2519830," + json.dumps(wanted["launch"]) + ");await SteamClient.Apps.SpecifyCompatTool(2519830," + json.dumps(wanted["compat"]) + ");return true})()")
            for _ in range(20):
                if self.read() == wanted:
                    return
                time.sleep(0.2)
            raise SetupError(tr('steam_apply_incomplete'))
        if steam_running(self.steam):
            raise SetupError(tr('steam_must_close'))
        for path, keys, changes in [
            (self.config, ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", APP_ID), {"name": wanted["compat"], "config": "", "priority": "250"}),
            (self.local, ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", APP_ID), {"LaunchOptions": wanted["launch"]}),
        ]:
            original = path.read_bytes()
            data = vdf_load(original.decode())
            subtree(data, *keys, create=True).update(changes)
            if path.read_bytes() != original or steam_running(self.steam):
                raise SetupError(tr('steam_race'))
            if transaction:
                transaction.put_vdf(path, keys, changes)
            else:
                atomic(path, vdf_dump(data).encode(), path.stat().st_mode & 0o777)
        if self.read() != wanted:
            raise SetupError(tr('steam_verify_failed'))


def launch_options(previous, helper):
    prefix = shlex.quote(str(helper))
    if str(helper) in previous:
        if previous.startswith(prefix + " %command%"):
            return previous if "-Device" in previous else previous + " -Device SteamVR"
        raise SetupError(tr('launch_options_unrecognized'))
    if "%command%" in previous:
        if previous.strip() != "%command%":
            raise SetupError(tr('launch_options_custom'))
        previous = ""
    return prefix + " %command%" + (" " + previous.strip() if previous.strip() else "") + ("" if "-Device" in previous else " -Device SteamVR")


def patch_bootstrap(original, runtime_version):
    version_key(runtime_version)
    old, new = 'DOTNET_ROOT="$PWD/dotnet-runtime"', 'DOTNET_ROOT="$PWD/dotnet-runtime-arm64"'
    if original.count(old) == 1 and new not in original:
        original = original.replace(old, new, 1)
    elif original.count(new) != 1 or old in original:
        raise SetupError(tr('bootstrap_unsupported'))
    # Match only the known dotnet-install call, allowing future runtime versions.
    pattern = (r'(?m)^(?P<prefix>[ \t]*bash "\$DOTNET_INSTALL_SCRIPT" --verbose )'
               r'--(?:channel [0-9]+\.[0-9]+|version [0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)'
               r' --runtime dotnet(?: --architecture arm64)? --install-dir "\$DOTNET_ROOT"[ \t]*$')
    matches = list(re.finditer(pattern, original))
    if len(matches) != 1:
        raise SetupError(tr('bootstrap_unsupported'))
    original = re.sub(pattern, lambda m: m['prefix'] + f'--version {runtime_version} --runtime dotnet --architecture arm64 --install-dir "$DOTNET_ROOT"', original)
    command = '"$DOTNET_EXECUTABLE" "$@"'
    prefix = ('OPENSSL_MODULES="$PWD/frame-libs-arm64/ossl-modules" '
              'OPENSSL_CONF="$PWD/frame-libs-arm64/openssl.cnf" '
              'LD_LIBRARY_PATH="$PWD/frame-libs-arm64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ')
    if original.count(prefix + command) == 1:
        return original
    if original.count(command) != 1:
        raise SetupError(tr('bootstrap_dotnet_missing'))
    return original.replace(command, prefix + command, 1)


def snapshot(path):
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.is_file():
        return {"kind": "file", "sha256": digest(path.read_bytes()), "mode": path.stat().st_mode & 0o777}
    if path.exists():
        raise SetupError(tr('path_is_directory', path=path))
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
    def __init__(self, root, metadata, previous=None):
        self.root = root
        self.path = root/"manifest.json"
        self.data = {"version": VERSION, "status": "applying", "files": json.loads(json.dumps(previous["files"])) if previous else [], **metadata}
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
        entry = next((e for e in self.data["files"] if e["path"] == str(path)), None)
        if entry:
            if entry.get("type") == "vdf" or before != entry["after"]:
                raise SetupError(tr('upgrade_file_changed', path=path))
            entry.setdefault("previous_after", []).append(before)
            entry.update(after=after, applied=False)
        else:
            entry = {"path": str(path), "before": before, "after": after, "applied": False}
            if before["kind"] == "file":
                backup = self.backups/str(len(self.data["files"]))
                atomic(backup, path.read_bytes(), 0o600)
                entry["backup"] = str(backup)
            self.data["files"].append(entry)
        self.save()  # Journal before the write, including a recoverable original.
        if snapshot(path) != before:
            raise SetupError(tr('install_file_race', path=path))
        atomic(path, data, mode)
        entry["applied"] = True
        self.save()

    def put_vdf(self, path, keys, changes):
        original = path.read_bytes()
        before = vdf_values(path, keys, changes)
        if before == changes:
            return
        entry = next((e for e in self.data["files"] if e["path"] == str(path)), None)
        if entry:
            if entry.get("type") != "vdf" or tuple(entry["keys"]) != tuple(keys) or before != entry["after"]:
                raise SetupError(tr('upgrade_settings_changed'))
            entry.setdefault("previous_after", []).append(before)
            entry.update(after=changes, applied=False)
        else:
            backup = self.backups/str(len(self.data["files"]))
            atomic(backup, original, 0o600)
            entry = {"type": "vdf", "path": str(path), "keys": keys, "before": before,
                     "after": changes, "backup": str(backup), "backup_sha256": digest(original), "applied": False}
            self.data["files"].append(entry)
        self.save()
        if path.read_bytes() != original:
            raise SetupError(tr('steam_write_race'))
        atomic(path, vdf_edit(original, keys, changes), path.stat().st_mode & 0o777)
        entry["applied"] = True
        self.save()


def validate_restore(data):
    # Verify every file before restoring any, avoiding a partially forced rollback.
    for entry in data["files"]:
        current = entry_value(entry)
        if current not in [entry["after"], entry["before"], *entry.get("previous_after", [])]:
            raise SetupError(tr('restore_file_changed', path=entry['path']))
        expected_backup = entry.get("backup_sha256") if entry.get("type") == "vdf" else entry["before"].get("sha256")
        if expected_backup and digest(Path(entry["backup"]).read_bytes()) != expected_backup:
            raise SetupError(tr('backup_changed'))


def restore_files(data):
    validate_restore(data)
    for entry in reversed(data["files"]):
        path = Path(entry["path"])
        current = entry_value(entry)
        if current == entry["before"]:
            continue
        if current not in [entry["after"], *entry.get("previous_after", [])]:
            raise SetupError(tr('restore_file_race', path=path))
        if entry.get("type") == "vdf":
            if "steam" in data and steam_running(Path(data["steam"])):
                raise SetupError(tr('restore_steam_running'))
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
        raise SetupError(tr('download_https_required'))
    if destination.is_file() and expected and hashlib.new(algorithm, destination.read_bytes()).hexdigest() == expected.lower():
        return
    request = urllib.request.Request(url, headers={"User-Agent": f"resonite-steam-frame/{VERSION}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(256 * 1024 * 1024 + 1)
    if len(data) > 256 * 1024 * 1024:
        raise SetupError(tr('download_too_large'))
    if expected and hashlib.new(algorithm, data).hexdigest() != expected.lower():
        raise SetupError(tr('download_hash_mismatch'))
    atomic(destination, data)


def unpack(archive, target, strip=0):
    with tarfile.open(archive) as bundle:
        total = 0
        for member in bundle:
            parts = Path(member.name).parts[strip:]
            if not parts:
                continue
            if member.name.startswith("/") or ".." in Path(member.name).parts or not (member.isfile() or member.isdir()):
                raise SetupError(tr('archive_unsafe'))
            total += member.size
            if total > 1024 * 1024 * 1024:
                raise SetupError(tr('archive_too_large'))
            destination = target.joinpath(*parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                atomic(destination, bundle.extractfile(member).read(), 0o755 if member.mode & 0o111 else 0o644)


def dotnet_download_file(release):
    return next(f for f in release["runtime"]["files"] if f["rid"] == "linux-arm64" and f["name"] == "dotnet-runtime-linux-arm64.tar.gz")


def version_key(version):
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?", version)
    if not match:
        raise SetupError(tr('runtime_version_invalid'))
    pre = tuple((0, int(part)) if part.isdecimal() else (1, part) for part in (match[4] or '').split('.'))
    return tuple(int(match[n]) for n in (1, 2, 3)) + (1 if match[4] is None else 0, pre)


def runtime_requirement(game):
    options = json.loads((game/"Renderite.Host.runtimeconfig.json").read_text())["runtimeOptions"]
    frameworks = options.get("frameworks", []) + ([options["framework"]] if "framework" in options else [])
    if len(frameworks) != 1 or frameworks[0].get("name") != "Microsoft.NETCore.App":
        raise SetupError(tr('runtime_framework_unsupported'))
    framework = frameworks[0]
    version = framework["version"]
    version_key(version)
    policy = framework.get("rollForward", options.get("rollForward"))
    if policy is not None and (not isinstance(policy, str) or policy.casefold() not in ("disable", "latestpatch", "minor", "major", "latestminor", "latestmajor")):
        raise SetupError(tr('runtime_policy_unsupported'))
    # An exact prerelease install avoids approximating the host's preview selection.
    exact = '-' in version or (policy is not None and policy.casefold() == "disable")
    # An explicit rollForward takes precedence over legacy applyPatches.
    if policy is None:
        exact = exact or framework.get("applyPatches", options.get("applyPatches", True)) is False
    return {"version": version, "exact": exact}


def runtime_matches(version, requirement):
    key, minimum = version_key(version), version_key(requirement["version"])
    # Install the requested major/minor even when the host permits wider roll-forward.
    return version == requirement["version"] if requirement["exact"] else key[:2] == minimum[:2] and key[3] == 1 and key >= minimum


def local_runtime(game, requirement):
    root = game/"dotnet-runtime-arm64"
    if not (root/"dotnet").is_file() or not elf_arm64(root/"dotnet"):
        return None
    found = []
    for path in (root/"shared/Microsoft.NETCore.App").glob("*/libcoreclr.so"):
        try:
            if runtime_matches(path.parent.name, requirement) and elf_arm64(path):
                found.append(path.parent.name)
        except SetupError:
            continue
    return max(found, key=version_key) if found else None


def select_runtime_release(metadata, requirement):
    releases = []
    for release in metadata["releases"]:
        version = release.get("runtime", {}).get("version", "")
        try:
            if runtime_matches(version, requirement):
                dotnet_download_file(release)
                releases.append(release)
        except (SetupError, StopIteration):
            continue
    if not releases:
        raise SetupError(tr("runtime_unavailable", version=requirement["version"]))
    return max(releases, key=lambda r: version_key(r["runtime"]["version"]))


def stage_runtime(game, stage, cache, requirement):
    existing = local_runtime(game, requirement)
    if existing:
        return None, existing
    channel = '.'.join(requirement["version"].split('.')[:2])
    metadata = cache/f"dotnet-releases-{channel}.json"
    # Refresh metadata: a cached index must not hide a newly released runtime.
    download(f"https://builds.dotnet.microsoft.com/dotnet/release-metadata/{channel}/releases.json", metadata)
    release = select_runtime_release(json.loads(metadata.read_text()), requirement)
    version = release["runtime"]["version"]
    item = dotnet_download_file(release)
    parsed = urlparse(item["url"])
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (host.endswith(".microsoft.com") or host == "dotnetcli.azureedge.net"):
        raise SetupError(tr('runtime_url_invalid'))
    if not re.fullmatch(r"[0-9a-fA-F]{128}", item["hash"]):
        raise SetupError(tr("runtime_hash_missing"))
    archive = cache/f"dotnet-runtime-{version}-linux-arm64.tar.gz"
    print(tr("runtime_preparing", version=version), flush=True)
    download(item["url"], archive, item["hash"], "sha512")
    unpack(archive, stage)
    if not (stage/"dotnet").is_file() or not elf_arm64(stage/"dotnet") or not (stage/f"shared/Microsoft.NETCore.App/{version}/libcoreclr.so").is_file() or not elf_arm64(stage/f"shared/Microsoft.NETCore.App/{version}/libcoreclr.so"):
        raise SetupError(tr('runtime_arch_invalid'))
    return stage, version


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
            raise SetupError(tr('library_missing', pattern=pattern))
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
            raise SetupError(tr('library_arch_invalid', source=source))
        name = alias or source.name
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise SetupError(tr('library_path_invalid'))
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
            raise SetupError(tr('library_dependency_missing', source=source))
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
        raise SetupError(tr('mikk_probe_failed'))


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
        raise SetupError(tr('mikk_arch_invalid'))
    probe_mikk(destination)
    return destination


# yt-dlp is invoked by the native engine. Keep its updater from replacing the
# installer-managed ARM64 binary; Resonite's small-file rollback restores the
# same launcher from .backup. Arguments for normal extraction stay intact.
VIDEO_LAUNCHER = '''#!/bin/sh
set -eu
tools="${0%/*}/../dotnet-runtime-arm64/tools"
update=0
for arg do
    case "$arg" in -U|--update|--update-to|--update-to=*) update=1;; esac
done
if [ "$update" = 1 ]; then
    for arg do
        case "$arg" in -U|--update|--update-to|nightly|--update-to=nightly) ;;
            *) echo _VIDEO_UPDATE_REQUIRED_ >&2; exit 2;;
        esac
    done
    echo _VIDEO_UPDATE_MANAGED_
    exit 0
fi
PATH="$tools:$PATH"
export PATH
exec "$tools/yt-dlp_linux_aarch64" "$@"
'''

def video_launcher(language=None):
    messages = json.loads((LOCALES/f"{language}.json").read_text()) if language else MESSAGES
    return VIDEO_LAUNCHER.replace("_VIDEO_UPDATE_REQUIRED_", shlex.quote(messages["video_update_required"])).replace("_VIDEO_UPDATE_MANAGED_", shlex.quote(messages["video_update_managed"]))


CMP_PROBE = r'''
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <vector>
#include "compressonator.h"
int main() {
    std::vector<unsigned char> pixels(16 * 16 * 4);
    for (size_t i = 0; i < pixels.size(); i += 4) {
        pixels[i] = 80; pixels[i+1] = 140; pixels[i+2] = 210; pixels[i+3] = 255;
    }
    for (auto format : {CMP_FORMAT_BC1, CMP_FORMAT_BC3, CMP_FORMAT_BC7}) {
        CMP_Texture source = {}, encoded = {}, decoded = {};
        for (auto texture : {&source, &encoded, &decoded}) {
            texture->dwSize = sizeof(CMP_Texture);
            texture->dwWidth = texture->dwHeight = 16;
            texture->nBlockWidth = texture->nBlockHeight = 4;
            texture->nBlockDepth = 1;
        }
        source.format = decoded.format = CMP_FORMAT_RGBA_8888;
        source.dwPitch = decoded.dwPitch = 64;
        source.dwDataSize = pixels.size(); source.pData = pixels.data();
        encoded.format = format;
        encoded.dwDataSize = CMP_CalculateBufferSize(&encoded);
        if (encoded.dwDataSize != (format == CMP_FORMAT_BC1 ? 128 : 256)) return 1;
        std::vector<unsigned char> blocks(encoded.dwDataSize), result(pixels.size());
        encoded.pData = blocks.data();
        decoded.dwDataSize = result.size(); decoded.pData = result.data();
        CMP_CompressOptions options = {};
        options.dwSize = sizeof(options); options.fquality = 1;
        options.dwnumThreads = 1; options.bDisableMultiThreading = true;
        if (CMP_ConvertTexture(&source, &encoded, &options, nullptr) != CMP_OK) return 2;
        if (CMP_ConvertTexture(&encoded, &decoded, &options, nullptr) != CMP_OK) return 3;
        int error = 0;
        for (size_t i = 0; i < pixels.size(); ++i)
            error = std::max(error, std::abs(int(pixels[i]) - int(result[i])));
        if (error > 12) return 4;
        std::printf("Compressonator format=%d bytes=%u max_error=%d: OK\n", format, encoded.dwDataSize, error);
    }
}
'''


def patch_compressonator(source):
    header = source/"cmp_core/shaders/bc1_cmp.h"
    original = header.read_text()
    marker = "CMP_STATIC bool bc1ToggleSIMD(CGU_INT newExtension)\n{"
    end = "    return result;\n}\n#endif"
    if original.count(marker) != 1 or original.count(end) != 1:
        raise SetupError(tr('cmp_source_unsupported'))
    modified = original.replace(marker, marker + "\n#if defined(__aarch64__) || defined(_M_ARM64) || defined(CMP_ARM64_BUILD)\n"
        "    cpu_bc1ComputeBestEndpoints = _cpu_bc1ComputeBestEndpoints;\n"
        "    g_bc1FunctionPointersSet = true;\n"
        "    return newExtension == EXTENSION_NONE || newExtension == EXTENSION_COUNT;\n#else", 1)
    header.write_text(modified.replace(end, "    return result;\n#endif\n}\n#endif", 1))
    plugin = source/"applications/_plugins/common/pluginbase.h"
    original = plugin.read_text()
    if original.count("#include <memory>") != 1:
        raise SetupError(tr('cmp_header_unsupported'))
    plugin.write_text(original.replace("#include <memory>", "#include <cstdint>\n#include <memory>", 1))


def stage_compressonator(stage, cache):
    archive = cache/f"compressonator-{CMP_COMMIT}.tar.gz"
    download(CMP_URL, archive, CMP_SHA256)
    source, build = stage/"source", stage/"build"
    unpack(archive, source, strip=1)
    patch_compressonator(source)
    print(tr('cmp_preparing'), flush=True)
    with (stage/"build.log").open("w") as log:
        try:
            subprocess.run(["cmake", "-S", str(source), "-B", str(build),
                "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
                "-DCMAKE_SHARED_LINKER_FLAGS=-Wl,-z,defs", "-DBUILD_SHARED_LIBS=ON",
                "-DOPTION_ENABLE_ALL_APPS=OFF", "-DOPTION_BUILD_CMP_SDK=ON",
                "-DOPTION_CMP_QT=OFF", "-DOPTION_BUILD_GUI=OFF", "-DOPTION_BUILD_KTX2=OFF",
                "-DOPTION_BUILD_EXR=OFF", "-DOPTION_BUILD_INTERNAL_CMP_TEST=OFF",
                "-DENABLE_AVX512=OFF"], stdout=log, stderr=subprocess.STDOUT, check=True)
            subprocess.run(["cmake", "--build", str(build), "--target", "CMP_Compressonator", "-j2"],
                stdout=log, stderr=subprocess.STDOUT, check=True)
        except subprocess.CalledProcessError:
            log.flush()
            atomic(cache/"compressonator-build.log", (stage/"build.log").read_bytes(), 0o600)
            raise SetupError(tr('cmp_build_failed', log=cache / 'compressonator-build.log'))
    for name in CMP_LIBS:
        if not elf_arm64(build/"lib"/name):
            raise SetupError(tr('cmp_arch_invalid'))
    probe = stage/"probe.cpp"
    probe.write_text(CMP_PROBE)
    subprocess.run(["c++", str(probe), "-I" + str(source/"cmp_compressonatorlib"),
        "-L" + str(build/"lib"), "-lCMP_Compressonator", "-lCMP_Framework",
        "-Wl,-rpath," + str(build/"lib"), "-o", str(stage/"probe")], check=True)
    subprocess.run([str(stage/"probe")], env=dict(os.environ, LD_LIBRARY_PATH=str(build/"lib")), timeout=60, check=True)
    return build/"lib"


def stage_video(stage, cache):
    binary = cache/f"yt-dlp-{YTDLP_VERSION}-linux-aarch64"
    download(YTDLP_URL, binary, YTDLP_SHA256)
    atomic(stage/"yt-dlp_linux_aarch64", binary.read_bytes(), 0o755)
    archive = cache/f"deno-{DENO_VERSION}-aarch64.zip"
    download(DENO_URL, archive, DENO_SHA256)
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) != 1 or entries[0].filename != "deno" or entries[0].file_size > 128 * 1024 * 1024:
            raise SetupError(tr('deno_archive_invalid'))
        data = bundle.read("deno")
    if digest(data) != DENO_BINARY_SHA256:
        raise SetupError(tr('deno_hash_mismatch'))
    atomic(stage/"deno", data, 0o755)
    for name in ("yt-dlp_linux_aarch64", "deno"):
        if not elf_arm64(stage/name):
            raise SetupError(tr('video_arch_invalid', name=name))
        subprocess.run([str(stage/name), "--version"], capture_output=True, timeout=30, check=True)
    return stage


def helper_source(state_root):
    return ("#!/usr/bin/env python3\nimport sys\n" + f"sys.path.insert(0, {str(state_root/'lib')!r})\n" + "from resonite_frame import launch\n" + f"launch({str(state_root)!r})\n").encode()


def launch(state_root):
    try:
        state = json.loads((Path(state_root)/"manifest.json").read_text())
        load_language(steam_language(state["steam"]))
        if state["status"] != "installed":
            raise SetupError(tr('launch_incomplete'))
        game = Path(state["game"])
        for relative in state["required"]:
            if not (game/relative).is_file():
                raise SetupError(tr('launch_file_missing', relative=relative))
        script = game/"LinuxBootstrap.sh"
        text = script.read_text()
        requirement = runtime_requirement(game)
        if not local_runtime(game, requirement):
            raise SetupError(tr("runtime_reinstall", version=requirement["version"]))
        patched = patch_bootstrap(text, state["dotnet_version"])
        if patched != text:
            # A changed bootstrap needs installer review, retaining the original journal.
            raise SetupError(tr('launch_bootstrap_changed'))
        if len(sys.argv) < 2:
            raise SetupError(tr('launch_command_missing'))
        os.execvpe(sys.argv[1], sys.argv[1:], dict(os.environ, PROTON_SET_GAME_DRIVE="0", FEX_TSOENABLED="1"))
    except (OSError, ValueError, SetupError) as error:
        print(f"Resonite Frame: {error}", file=sys.stderr)
        raise SystemExit(1)


def check_report(game, app, state_root):
    requirement = runtime_requirement(game)
    runtime_version = local_runtime(game, requirement)
    result = {"game": str(game), "build": app.get("buildid"),
              "game_running": game_running(game), "installer_manifest": (state_root/"manifest.json").is_file(),
              "runtime_arm64": (game/"dotnet-runtime-arm64/dotnet").is_file() and elf_arm64(game/"dotnet-runtime-arm64/dotnet"),
              "runtime_required": requirement["version"], "runtime_version": runtime_version,
              "runtime_version_available": runtime_version is not None,
              "support_libraries": {name: (game/"frame-libs-arm64"/name).is_file() for name in ["libicuuc.so", "libssl.so.3", "libcrypto.so.3", "libpulse.so.0", "ossl-modules/legacy.so", "openssl.cnf"]},
              "mikktspace_arm64": (game/"runtimes/linux-arm64/native/libmikktspace.so").is_file() and elf_arm64(game/"runtimes/linux-arm64/native/libmikktspace.so")}
    result["video_tools_arm64"] = {name: (game/"dotnet-runtime-arm64/tools"/name).is_file() and elf_arm64(game/"dotnet-runtime-arm64/tools"/name) for name in ("yt-dlp_linux_aarch64", "deno")}
    result["video_launcher"] = (game/"RuntimeData/yt-dlp_linux").is_file() and (game/"RuntimeData/yt-dlp_linux").read_bytes() in [video_launcher(path.stem).encode() for path in LOCALES.glob("*.json")]
    result["compressonator_arm64"] = {name: (game/"runtimes/linux-arm64/native"/name).is_file() and elf_arm64(game/"runtimes/linux-arm64/native"/name) for name in CMP_LIBS}
    try:
        text = (game/"LinuxBootstrap.sh").read_text()
        pinned = re.search(r'--verbose --version ([0-9A-Za-z.-]+) --runtime dotnet --architecture arm64', text)
        version = pinned[1] if pinned else requirement["version"]
        patched = patch_bootstrap(text, version)
        result["bootstrap_supported"] = patched == text and runtime_matches(version, requirement) and local_runtime(game, {"version": version, "exact": True}) is not None
    except SetupError:
        result["bootstrap_supported"] = False
    ready = result["runtime_arm64"] and result["runtime_version_available"] and all(result["support_libraries"].values()) and result["mikktspace_arm64"] and result["bootstrap_supported"] and result["video_launcher"] and all(result["video_tools_arm64"].values()) and all(result["compressonator_arm64"].values())
    return result, 0 if ready else 1


def check(game, app, state_root):
    result, code = check_report(game, app, state_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def uninstall(state_root):
    path = state_root/"manifest.json"
    data = json.loads(path.read_text())
    if data["status"] == "uninstalled":
        print(tr('already_uninstalled'))
        return
    game, steam = Path(data["game"]), Path(data["steam"])
    if game_running(game):
        raise SetupError(tr('uninstall_game_running'))
    settings = Settings(steam, data["app"], data.get("user_id"))
    if data["settings_mode"] == "cdp":
        if settings.mode != "cdp":
            raise SetupError(tr('restore_steam_open_required'))
        current = settings.read()
        if any(current[k] not in (data["settings_after"][k], data["settings_before"][k]) for k in current):
            raise SetupError(tr('restore_launch_options_changed'))
        # Validate files before changing even the live settings.
        validate_restore(data)
        if current != data["settings_before"]:
            settings.apply(data["settings_before"], expected=current)
    elif settings.mode != "offline":
        raise SetupError(tr('restore_steam_closed_required'))
    restore_files(data)
    data["status"] = "uninstalled"
    atomic(path, (json.dumps(data, ensure_ascii=False, indent=2)+"\n").encode(), 0o600)
    print(tr('uninstall_complete'))


def install(args, steam, game, app, state_root):
    if game_running(game):
        raise SetupError(tr('install_game_running'))
    if app.get("StateFlags") != "4":
        raise SetupError(tr('game_updating'))
    requirement = runtime_requirement(game)
    runtime_config_before = (game/"Renderite.Host.runtimeconfig.json").read_bytes()
    for command in ("c++", "ldd", "cmake", "make"):
        if not shutil.which(command):
            raise SetupError(tr('command_missing', command=command))
    if not any(all((root/"steamapps/common/Proton - Experimental"/name).is_file() for name in ("proton", "toolmanifest.vdf")) for root in libraries(steam)):
        raise SetupError(tr('proton_missing'))
    script = game/"LinuxBootstrap.sh"
    original = script.read_bytes()
    patch_bootstrap(original.decode(), requirement["version"])  # Validate before staging.
    settings = Settings(steam, app, args.user_id)
    helper = (Path(args.bin_dir).expanduser().absolute() if args.bin_dir else Path.home()/".local/bin")/"frame-resonite-launch"
    wanted = {"launch": launch_options(settings.before["launch"], helper), "compat": "proton_experimental"}
    manifest = state_root/"manifest.json"
    previous = None
    if manifest.is_file():
        data = json.loads(manifest.read_text())
        if data["status"] == "installed" and data["game"] == str(game):
            changed = [e for e in data["files"] if entry_value(e) != e["after"]]
            # Steam may replace the bootstrap during a game update. A recognized
            # unpatched script becomes the restore point for this game version.
            replaced_bootstrap = (
                bool(changed)
                and all(e["path"] == str(script) and e.get("type") != "vdf" for e in changed)
                and original.decode().count('DOTNET_ROOT="$PWD/dotnet-runtime"') == 1
                and 'frame-libs-arm64' not in original.decode()
            )
            if (not changed or replaced_bootstrap) and settings.read() == data["settings_after"]:
                current = (
                    not replaced_bootstrap and data["version"] == VERSION
                    and runtime_matches(data["dotnet_version"], requirement)
                    and local_runtime(game, requirement)
                    and patch_bootstrap(original.decode(), data["dotnet_version"]).encode() == original
                )
                if current:
                    print(tr('already_installed'))
                    return
                if data["settings_mode"] != settings.mode or data["steam"] != str(steam):
                    raise SetupError(tr('upgrade_environment_changed'))
                previous = data
                if replaced_bootstrap:
                    previous["files"] = [e for e in previous["files"] if e["path"] != str(script)]
            else:
                raise SetupError(tr('managed_files_changed'))
        if not previous and data["status"] != "uninstalled":
            raise SetupError(tr('install_incomplete'))
        archived = state_root/f"manifest-previous-{time.time_ns()}.json"
        shutil.copy2(manifest, archived)
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = state_root/"cache"
    cache.mkdir(exist_ok=True, mode=0o700)
    print(tr('install_preparing'), flush=True)
    with tempfile.TemporaryDirectory(prefix="resonite-frame-") as temporary:
        stage = Path(temporary)
        support = stage/"support"
        support.mkdir()
        stage_host_libraries(support)
        runtime, runtime_version = stage_runtime(game, stage/"runtime", cache, requirement)
        patched = patch_bootstrap(original.decode(), runtime_version).encode()
        mikk = stage_mikk(game, stage/"mikk", cache)
        video = stage_video(stage/"video", cache)
        compressor = stage_compressonator(stage/"compressonator", cache)
        _, latest_app = find_game(steam, str(game))
        if game_running(game) or script.read_bytes() != original or (game/"Renderite.Host.runtimeconfig.json").read_bytes() != runtime_config_before or settings.read() != settings.before or latest_app.get("buildid") != app.get("buildid") or latest_app.get("StateFlags") != "4":
            raise SetupError(tr('install_staging_race'))
        tx = Transaction(state_root, {"game": str(game), "steam": str(steam), "build": app.get("buildid"), "app": {"LastOwner": app.get("LastOwner", "0")}, "user_id": settings.local.parents[1].name if settings.local else args.user_id,
                  "settings_mode": settings.mode, "settings_before": previous["settings_before"] if previous else settings.before, "settings_after": wanted, "required": [], "source_commit": MIKK_COMMIT, "source_sha256": MIKK_SHA256, "dotnet_version": runtime_version, "dotnet_required": requirement["version"],
                  "compressonator_commit": CMP_COMMIT, "yt_dlp_version": YTDLP_VERSION, "deno_version": DENO_VERSION}, previous=previous)
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
            for name in ("yt-dlp_linux_aarch64", "deno"):
                relative = Path("dotnet-runtime-arm64/tools")/name
                tx.put(game/relative, (video/name).read_bytes(), 0o755)
                tx.data["required"].append(str(relative))
            for name in ("yt-dlp_linux", "yt-dlp_linux.backup"):
                tx.put(game/"RuntimeData"/name, video_launcher().encode(), 0o755)
                tx.data["required"].append("RuntimeData/" + name)
            for name in CMP_LIBS:
                relative = Path("runtimes/linux-arm64/native")/name
                tx.put(game/relative, (compressor/name).read_bytes())
                tx.data["required"].append(str(relative))
            tx.data["required"] += ["dotnet-runtime-arm64/dotnet", "runtimes/linux-arm64/native/libmikktspace.so"]
            tx.put(script, patched, script.stat().st_mode & 0o777)
            tx.put(state_root/"lib/resonite_frame.py", Path(__file__).read_bytes())
            for locale in sorted(LOCALES.glob("*.json")):
                tx.put(state_root/"lib/locales"/locale.name, locale.read_bytes())
            tx.put(helper, helper_source(state_root), 0o755)
            tx.save()
            if game_running(game):
                raise SetupError(tr('install_game_race'))
            settings.apply(wanted, tx, expected=settings.before)
            tx.data["status"] = "installed"
            tx.save()
        except BaseException:
            tx.data["status"] = "failed"
            tx.save()
            # Keep the journal, including partially applied Steam API changes.
            print(tr('install_failed', manifest=manifest), file=sys.stderr)
            raise
    print(tr('install_complete'))


def menu_action(state_root):
    path = state_root/"manifest.json"
    if not path.is_file() or json.loads(path.read_text()).get("status") == "uninstalled":
        return "install"
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise SetupError(tr("menu_terminal"))
    if not shutil.which("whiptail"):
        raise SetupError(tr("menu_missing"))
    status = json.loads(path.read_text()).get("status")
    items = [] if status in ("failed", "applying") else ["install", tr("menu_install")]
    items += ["check", tr("menu_check"), "uninstall", tr("menu_uninstall"), "exit", tr("menu_exit")]
    result = subprocess.run(["whiptail", "--title", tr("menu_title"), "--ok-button", tr("menu_select"), "--cancel-button", tr("menu_exit"),
        "--menu", tr("menu_recover") if status in ("failed", "applying") else tr("menu_prompt"),
        "18", "76", "6", *items], stdout=sys.stderr, stderr=subprocess.PIPE, text=True)
    if result.returncode in (1, 255):
        return "exit"
    if result.returncode or result.stderr.strip() not in ("install", "check", "uninstall", "exit"):
        raise SetupError(tr("menu_failed"))
    return result.stderr.strip()


def show_check(result, code):
    def status(value):
        return tr("check_ok") if value else tr("check_missing")
    lines = [tr("check_ready") if code == 0 else tr("check_incomplete"), "",
        tr("check_runtime", required=result["runtime_required"], installed=result["runtime_version"] or tr("check_none"))]
    for key in ("runtime_arm64", "bootstrap_supported", "mikktspace_arm64", "video_launcher"):
        lines.append(tr("check_item", name=tr("check_" + key), status=status(result[key])))
    for key in ("support_libraries", "video_tools_arm64", "compressonator_arm64"):
        lines.append(tr("check_item", name=tr("check_" + key), status=status(all(result[key].values()))))
        lines.extend("  " + name for name, exists in result[key].items() if not exists)
    subprocess.run(["whiptail", "--title", tr("menu_check"), "--ok-button", tr("menu_close"), "--scrolltext", "--msgbox", "\n".join(lines), "22", "78"], stdout=sys.stderr, check=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--steam-root")
    early.add_argument("--language")
    hints, _ = early.parse_known_args(argv)
    load_language(hints.language or steam_language(hints.steam_root))
    parser = argparse.ArgumentParser(description=tr("help_description"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--install", action="store_true", help=tr("help_install"))
    modes.add_argument("--check", action="store_true", help=tr("help_check"))
    modes.add_argument("--uninstall", action="store_true", help=tr("help_uninstall"))
    parser.add_argument("--steam-root", help=tr("help_steam_root"))
    parser.add_argument("--game-dir", help=tr("help_game_dir"))
    parser.add_argument("--state-dir", help=tr("help_state_dir"))
    parser.add_argument("--bin-dir", help=tr("help_bin_dir"))
    parser.add_argument("--user-id", help=tr("help_user_id"))
    parser.add_argument("--language", choices=("ja", "en"), help=tr("help_language"))
    args = parser.parse_args(argv)
    state_root = Path(args.state_dir).expanduser().absolute() if args.state_dir else Path(os.environ.get("XDG_DATA_HOME", str(Path.home()/".local/share")))/"resonite-steam-frame"
    try:
        if platform.system() != "Linux" or platform.machine().lower() not in ("aarch64", "arm64"):
            raise SetupError(tr('platform_unsupported'))
        if os.geteuid() == 0:
            raise SetupError(tr('root_unsupported'))
        interactive = not (args.install or args.check or args.uninstall)
        action = "uninstall" if args.uninstall else "check" if args.check else "install" if args.install else menu_action(state_root)
        if action == "exit":
            return 0
        if action == "uninstall":
            if not (state_root/"manifest.json").is_file():
                raise SetupError(tr('manifest_missing'))
            with locked_state(state_root):
                uninstall(state_root)
            return 0
        steam = steam_root(args.steam_root)
        game, app = find_game(steam, args.game_dir)
        if action == "check":
            if interactive:
                result, code = check_report(game, app, state_root)
                show_check(result, code)
                return code
            return check(game, app, state_root)
        with locked_state(state_root):
            install(args, steam, game, app, state_root)
        return 0
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError, SetupError) as error:
        print(f"Resonite Frame: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
