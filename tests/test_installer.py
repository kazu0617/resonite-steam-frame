import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
import resonite_frame as rf

BOOTSTRAP = '''#!/bin/sh
DOTNET_ROOT="$PWD/dotnet-runtime"
dotnet() { "$DOTNET_EXECUTABLE" "$@"; }
bash "$DOTNET_INSTALL_SCRIPT" --verbose --channel 10.0 --runtime dotnet --install-dir "$DOTNET_ROOT"
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_bootstrap_known_and_manual_patch_are_idempotent(self):
        modified = rf.patch_bootstrap(BOOTSTRAP)
        self.assertEqual(rf.patch_bootstrap(modified), modified)
        self.assertIn('--version 10.0.15', modified)
        self.assertIn('OPENSSL_MODULES=', modified)
        manual = BOOTSTRAP.replace('dotnet-runtime"', 'dotnet-runtime-arm64"').replace('--runtime dotnet --install-dir', '--runtime dotnet --architecture arm64 --install-dir')
        self.assertEqual(rf.patch_bootstrap(manual), modified)

    def test_changed_bootstrap_fails_without_guessing(self):
        with self.assertRaises(rf.SetupError):
            rf.patch_bootstrap(BOOTSTRAP.replace('--channel 10.0', '--channel 11.0'))

    def test_vdf_roundtrip_quotes_backslashes_and_unicode(self):
        original = {'root': {'path': 'C:\\ゲーム\\models', 'quote': '"hello"', 'newline': 'a\nb', 'unknown': '\\q'}}
        self.assertEqual(rf.vdf_load(rf.vdf_dump(original)), original)
        self.assertEqual(rf.vdf_load('// comment\n"root" { "item" "// not a comment" }'), {'root': {'item': '// not a comment'}})

    def test_vdf_rejects_duplicate_keys_and_truncated_input(self):
        for text in ['"x" "1" "X" "2"', '"x" { "y" "z"', '"x" }']:
            with self.subTest(text=text), self.assertRaises(rf.SetupError):
                rf.vdf_load(text)

    def test_transaction_idempotence_and_exact_file_restoration(self):
        old = self.root/'old'
        old.write_bytes(b'original')
        old.chmod(0o640)
        new = self.root/'new'
        tx = rf.Transaction(self.root/'state', {})
        tx.put(old, b'patched')
        tx.put(new, b'new')
        tx.put(new, b'new')
        self.assertEqual(len(tx.data['files']), 2)
        rf.restore_files(tx.data)
        self.assertEqual(old.read_bytes(), b'original')
        self.assertEqual(old.stat().st_mode & 0o777, 0o640)
        self.assertFalse(new.exists())

    def test_restore_refuses_user_edits_before_changing_any_file(self):
        first, second = self.root/'first', self.root/'second'
        first.write_bytes(b'one')
        second.write_bytes(b'two')
        tx = rf.Transaction(self.root/'state', {})
        tx.put(first, b'patched-one')
        tx.put(second, b'patched-two')
        first.write_bytes(b'user edit')
        with self.assertRaises(rf.SetupError):
            rf.restore_files(tx.data)
        self.assertEqual(second.read_bytes(), b'patched-two')

    def test_vdf_restore_preserves_later_other_app_changes(self):
        path = self.root/'config.vdf'
        path.write_text(rf.vdf_dump({'apps': {'2519830': {'LaunchOptions': '-old'}, '438100': {'value': 'before'}}}))
        tx = rf.Transaction(self.root/'state', {})
        tx.put_vdf(path, ('apps', '2519830'), {'LaunchOptions': 'new'})
        data = rf.vdf_load(path.read_text())
        data['apps']['438100']['value'] = 'changed later'
        data['apps']['2519830']['LastPlayed'] = '1234'
        path.write_text(rf.vdf_dump(data))
        rf.restore_files(tx.data)
        restored = rf.vdf_load(path.read_text())
        self.assertEqual(restored['apps']['2519830'], {'LaunchOptions': '-old', 'LastPlayed': '1234'})
        self.assertEqual(restored['apps']['438100']['value'], 'changed later')

    def test_backup_paths_remain_distinct_across_reinstalls(self):
        path = self.root/'file'
        path.write_bytes(b'original')
        first = rf.Transaction(self.root/'state', {})
        first.put(path, b'one')
        rf.restore_files(first.data)
        second = rf.Transaction(self.root/'state', {})
        second.put(path, b'two')
        self.assertNotEqual(first.data['files'][0]['backup'], second.data['files'][0]['backup'])
        self.assertEqual(Path(first.data['files'][0]['backup']).read_bytes(), b'original')

    def test_archive_rejects_traversal_and_links(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('top/../../escape', tarfile.REGTYPE), ('top/link', tarfile.SYMTYPE)]:
            archive = self.root/'archive.tar'
            with tarfile.open(archive, 'w') as bundle:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.REGTYPE:
                    info.size = 1
                    bundle.addfile(info, io.BytesIO(b'x'))
                else:
                    info.linkname = '/tmp'
                    bundle.addfile(info)
            with self.subTest(name=name), self.assertRaises(rf.SetupError):
                rf.unpack(archive, self.root/'extracted', strip=1)

    def test_keeps_existing_game_arguments_and_refuses_custom_wrapper(self):
        helper = Path('/home/test/.local/bin/frame-resonite-launch')
        wanted = rf.launch_options('-Screen -Verbose', helper)
        self.assertIn('%command% -Screen -Verbose', wanted)
        self.assertEqual(rf.launch_options(wanted, helper), wanted)
        with self.assertRaises(rf.SetupError):
            rf.launch_options('other-wrapper %command% -Verbose', helper)

    def test_game_running_guard_precedes_every_write(self):
        with patch.object(rf, 'game_running', return_value=True):
            with self.assertRaises(rf.SetupError):
                rf.install(None, self.root, self.root, {}, self.root/'state')
        self.assertFalse((self.root/'state').exists())

    def test_ldd_loader_paths_and_core_libraries_are_never_staged(self):
        output = '''
        /lib/ld-linux-aarch64.so.1 => /lib/ld-linux-aarch64.so.1 (0x1)
        libc.so.6 => /usr/lib/libc.so.6 (0x2)
        libm.so.6 => /usr/lib/libm.so.6 (0x3)
        libstdc++.so.6 => /usr/lib/libstdc++.so.6 (0x4)
        libpulsecommon-17.0.so => /usr/lib/pulseaudio/libpulsecommon-17.0.so (0x5)
        '''
        self.assertEqual(list(rf.native_dependencies(output)), [(Path('/usr/lib/pulseaudio/libpulsecommon-17.0.so'), 'libpulsecommon-17.0.so')])

    def test_wrong_architecture_fails_without_creating_state(self):
        with patch.object(rf.platform, 'machine', return_value='x86_64'):
            self.assertEqual(rf.main(['--state-dir', str(self.root/'state')]), 1)
        self.assertFalse((self.root/'state').exists())

    def test_concurrent_state_changes_are_rejected(self):
        with rf.locked_state(self.root/'state'):
            with self.assertRaises(rf.SetupError):
                with rf.locked_state(self.root/'state'):
                    self.fail('Acquired an already held lock')
        with rf.locked_state(self.root/'state'):
            pass

    def test_dotnet_download_selects_runtime_instead_of_apphost_pack(self):
        files = [{'name': 'dotnet-apphost-pack-linux-arm64.tar.gz', 'rid': 'linux-arm64'}, {'name': 'dotnet-runtime-linux-arm64.tar.gz', 'rid': 'linux-arm64'}]
        self.assertIs(rf.dotnet_download_file({'runtime': {'files': files}}), files[1])


if __name__ == '__main__':
    unittest.main()
