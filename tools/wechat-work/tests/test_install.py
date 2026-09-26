import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_work_reader', SOURCE / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / 'home with spaces'
        self.home.mkdir()
        self.binary = self.root / 'fake-wx-cli'
        self.binary.write_text('#!/bin/sh\necho "wx-cli 0.7.4"\n')
        self.binary.chmod(0o700)
        self.account = self.root / 'account'
        (self.account / 'db_storage').mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def install(self, **kwargs):
        return installer.install(SOURCE, self.home, self.binary, self.account, **kwargs)

    def test_shared_entrypoints_private_config_and_repeat_install(self):
        self.install()
        runtime = self.home / '.local/share/wechat-work'
        config_path = runtime / 'config.json'
        config = json.loads(config_path.read_text())
        self.assertEqual(config['account_dir'], str(self.account))
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
        for host in ('.codex', '.claude'):
            self.assertEqual((self.home / host / 'skills/wechat-work').resolve(), runtime / 'skill')
        launcher = self.home / '.local/bin/wechat-work'
        result = subprocess.run([str(launcher), '--version'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), '0.1.0')
        # Updating needs no dependency on the legacy wx reader or its config.
        installer.install(SOURCE, self.home)
        self.assertEqual(json.loads(config_path.read_text()), config)
        self.assertFalse((self.home / '.claude/commands/wx-image.md').exists())

    def test_legacy_image_command_only_replaced_with_explicit_option(self):
        command = self.home / '.claude/commands/wx-image.md'
        command.parent.mkdir(parents=True)
        command.write_text('keep original')
        self.install()
        self.assertEqual(command.read_text(), 'keep original')
        result = self.install(replace_wx_image=True)
        backups = list(Path(result['backup_dir']).iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'keep original')
        self.assertIn('wechat-work images', command.read_text())

    def test_unrelated_skill_blocks_before_replacing_entrypoint(self):
        target = self.home / '.codex/skills/wechat-work'
        target.mkdir(parents=True)
        (target / 'SKILL.md').write_text('user skill')
        with self.assertRaisesRegex(ValueError, 'Existing unrelated skill'):
            self.install()
        self.assertEqual((target / 'SKILL.md').read_text(), 'user skill')
        self.assertFalse((self.home / '.local/bin/wechat-work').exists())

    def test_symlink_parent_is_refused(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.home / '.local').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink directory'):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_first_install_requires_explicit_account(self):
        with self.assertRaisesRegex(ValueError, '--account-dir'):
            installer.install(SOURCE, self.home, self.binary)
        self.assertFalse((self.home / '.local/bin/wechat-work').exists())


if __name__ == '__main__':
    unittest.main()
