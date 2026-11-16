#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the real install template with fake external commands; never touches CoreAudio."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent


class InstallFlowTests(unittest.TestCase):
    def run_case(self, ready, answer, reload_fails=False, query_fails=False):
        # Intentionally retain test evidence; do not automatically delete files.
        root = Path(tempfile.mkdtemp(prefix='recording-bean-installer-test-'))
        log = root / 'actions.log'
        sudo = root / 'fake-sudo'
        sudo.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$RBA_TEST_LOG"
if [[ "$*" == *launchctl* ]]; then
    if [ "$RBA_TEST_RELOAD_FAILS" = 1 ]; then exit 1; fi
    touch "$RBA_TEST_ROOT/ready"
fi
''')
        noop = root / 'noop'
        noop.write_text('#!/bin/bash\nexit 0\n')
        checker = root / 'check-device'
        checker.write_text('#!/bin/bash\nif [ "$RBA_TEST_QUERY_FAILS" = 1 ]; then exit 2; fi\ntest -f "$RBA_TEST_ROOT/ready"\n')
        for file in (sudo, noop, checker): file.chmod(0o755)
        if ready: (root / 'ready').touch()
        text = (TOOLS / 'install.command.in').read_text()
        assert text.count('/usr/bin/sudo') == 2
        text = text.replace('/usr/bin/sudo', '"' + str(sudo) + '"')
        text = text.replace('/usr/bin/shasum', '"' + str(noop) + '"')
        text = text.replace('/bin/sleep', '"' + str(noop) + '"')
        text = text.replace('/Library/Audio/Plug-Ins/HAL/RecordingBeanAudio2ch.driver/Contents/Helpers/check-device', str(checker))
        text = text.replace('@PACKAGE_NAME@', 'test.pkg').replace('@PACKAGE_HASH@', 'test').replace('@CHECK_HASH@', 'test')
        script = root / 'install.command'; script.write_text(text)
        env = dict(os.environ, RBA_TEST_ROOT=str(root), RBA_TEST_LOG=str(log),
                   RBA_TEST_RELOAD_FAILS='1' if reload_fails else '0',
                   RBA_TEST_QUERY_FAILS='1' if query_fails else '0')
        result = subprocess.run(['/bin/bash', str(script)], input=answer, text=True,
                                capture_output=True, env=env, timeout=10)
        return result, log.read_text().splitlines()

    def test_already_ready_needs_no_restart_or_prompt(self):
        result, calls = self.run_case(True, '')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn('声卡已就绪', result.stdout)
        self.assertNotIn('现在可以重新加载', result.stdout)

    def test_decline_keeps_installed_without_restarting(self):
        result, calls = self.run_case(False, 'n\n')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('声卡已就绪', result.stdout)

    def test_eof_is_not_consent(self):
        result, calls = self.run_case(False, '')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(calls), 1)

    def test_explicit_yes_restarts_only_exact_audio_service(self):
        result, calls = self.run_case(False, 'y\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], '/bin/launchctl kickstart -k system/com.apple.audio.coreaudiod')
        self.assertIn('声卡已就绪', result.stdout)

    def test_reload_failure_never_claims_ready_or_tries_another_kill(self):
        result, calls = self.run_case(False, 'y\n', reload_fails=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('声卡已就绪', result.stdout)

    def test_query_error_does_not_trigger_restart_even_if_input_is_yes(self):
        result, calls = self.run_case(False, 'y\n', query_fails=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
