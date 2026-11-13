"""Local adapter contracts; no authenticated model requests are made."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).parents[1] / "providers.py").exists(), "provider implementation is missing")
        self.p = importlib.import_module("backend.providers")

    def parse(self, provider, events):
        parser = self.p._ResponseParser(provider)
        for event in events:
            parser.feed(event)
        return parser.finish()

    def test_codex_returns_final_assistant_only(self):
        self.assertEqual(self.parse("codex", [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "working", "phase": "commentary"}},
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "ignore"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "final summary"}},
            {"type": "turn.completed"},
        ]), "final summary")

    def test_codex_error_and_truncated_turn_are_not_success(self):
        for events in ([{"type": "turn.failed", "error": {"message": "not logged in"}}],
                       [{"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}]):
            with self.subTest(events=events), self.assertRaises(self.p.ProviderError):
                self.parse("codex", events)

    def test_claude_prefers_result_without_duplicating_assistant(self):
        self.assertEqual(self.parse("claude", [
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "summary"}]}},
            {"type": "result", "subtype": "success", "result": "summary"},
        ]), "summary")

    def test_claude_errors_even_when_partial_text_exists(self):
        with self.assertRaisesRegex(self.p.ProviderError, "maximum turns"):
            self.parse("claude", [{"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
                                  {"type": "result", "subtype": "error_max_turns", "is_error": True, "errors": ["maximum turns"]}])

    def test_commands_preserve_guards_and_do_not_hardcode_model(self):
        cwd = Path("/tmp/recording-test-job")
        codex = self.p._build_command("codex", "/bin/codex", cwd, "")
        self.assertEqual(codex[codex.index("--sandbox") + 1], "read-only")
        self.assertNotIn("--model", codex)
        for unsafe in ("--ignore-rules", "--ignore-user-config", "--dangerously-bypass-approvals-and-sandbox", "features.hooks=false"):
            self.assertNotIn(unsafe, codex)
        claude = self.p._build_command("claude", "/bin/claude", cwd, "my-model")
        self.assertEqual(claude[claude.index("--tools") + 1], "")
        self.assertEqual(claude[claude.index("--max-turns") + 1], "1")
        self.assertEqual(claude[claude.index("--model") + 1], "my-model")
        self.assertNotIn("bypassPermissions", claude)

    def test_environment_strips_credentials_and_parent_identity(self):
        env = {"PATH": "/bin", "HOME": "/home/user", "CODEX_HOME": "/home/user/.codex",
               "CODEX_THREAD_ID": "parent", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "parent",
               "OPENAI_API_KEY": "not-for-child", "OPENAI_BASE_URL": "proxy",
               "ANTHROPIC_API_KEY": "not-for-child", "ANTHROPIC_AUTH_TOKEN": "not-for-child",
               "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "parent", "CLAUDE_CODE_OAUTH_TOKEN": "not-for-child"}
        for provider in ("codex", "claude"):
            result = self.p._child_environment(provider, env)
            self.assertEqual(result, {"PATH": "/bin", "HOME": "/home/user", "CODEX_HOME": "/home/user/.codex"})

    async def test_runner_uses_stdin_and_separate_process_group(self):
        result = []
        source = "import sys,json; print(json.dumps({'type':'echo','text':sys.stdin.read()}))"
        await self.p._run_jsonl([sys.executable, "-c", source], stdin="秘密\ntranscript", cwd=Path(tempfile.mkdtemp()),
                                env=dict(os.environ), on_event=result.append, timeout=3)
        self.assertEqual(result, [{"type": "echo", "text": "秘密\ntranscript"}])

    async def test_runner_enforces_output_bound(self):
        with self.assertRaisesRegex(self.p.ProviderError, "输出"):
            await self.p._run_jsonl([sys.executable, "-c", "print('x'*5000)"], stdin="", cwd=Path(tempfile.mkdtemp()),
                                    env=dict(os.environ), on_event=lambda _: None, timeout=3, max_output_bytes=100)

    async def test_timeout_reaps_process(self):
        events = []
        source = "import os,json,time; print(json.dumps({'pid':os.getpid()}),flush=True); time.sleep(20)"
        with self.assertRaisesRegex(self.p.ProviderError, "超时"):
            await self.p._run_jsonl([sys.executable, "-c", source], stdin="", cwd=Path(tempfile.mkdtemp()),
                                    env=dict(os.environ), on_event=events.append, timeout=2)
        self.assertTrue(events)
        with self.assertRaises(ProcessLookupError):
            os.kill(events[0]["pid"], 0)

    async def test_timeout_stops_descendant_after_leader_exits(self):
        events = []
        source = ("import os,json,subprocess,sys; "
                  "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
                  "print(json.dumps({'child':child.pid}),flush=True)")
        with patch.object(self.p.os, "killpg", wraps=os.killpg) as killpg:
            with self.assertRaises(self.p.ProviderError):
                await self.p._run_jsonl([sys.executable, "-c", source], stdin="", cwd=Path(tempfile.mkdtemp()),
                                        env=dict(os.environ), on_event=events.append, timeout=2)
        self.assertTrue(events)
        signals = [call.args[1] for call in killpg.call_args_list]
        self.assertEqual(signals[0], signal.SIGTERM)
        self.assertIn(signal.SIGKILL, signals)

    async def test_cleanup_retries_transient_macos_group_permission_race(self):
        class Finished:
            pid = 123456
            async def wait(self):
                return 0
        with patch.object(self.p.os, "killpg", side_effect=[None, PermissionError(), ProcessLookupError()]) as killpg:
            await self.p._terminate_process_group(Finished())
        self.assertEqual(killpg.call_count, 3)

    async def test_runner_drains_stderr_while_feeding_large_stdin(self):
        events = []
        source = "import sys,json; sys.stderr.write('warning'*20000); sys.stderr.flush(); print(json.dumps({'length':len(sys.stdin.read())}))"
        await self.p._run_jsonl([sys.executable, "-c", source], stdin="a" * 200000, cwd=Path(tempfile.mkdtemp()),
                                env=dict(os.environ), on_event=events.append, timeout=3)
        self.assertEqual(events, [{"length": 200000}])

    async def test_summary_preserves_structured_error_on_nonzero_exit(self):
        async def fake_run(argv, **kwargs):
            kwargs["on_event"]({"type": "turn.failed", "error": {"message": "model unavailable"}})
            raise self.p.ProviderError("generic exit error")
        with patch.object(self.p, "_resolve_binary", return_value="/bin/codex"), patch.object(self.p, "_run_jsonl", side_effect=fake_run):
            with self.assertRaisesRegex(self.p.ProviderError, "model unavailable"):
                await self.p.summarize("codex", "transcript", work_dir=Path(tempfile.mkdtemp()))

    def test_errors_redact_credential_shaped_values(self):
        with self.assertRaises(self.p.ProviderError) as raised:
            self.parse("codex", [{"type": "error", "message": "Authorization: Bearer secret-value sk-abcd api_key=other-secret"}])
        message = str(raised.exception)
        self.assertNotIn("secret-value", message)
        self.assertNotIn("sk-abcd", message)
        self.assertNotIn("other-secret", message)

    async def test_cancel_reaps_process_and_propagates(self):
        ready = asyncio.Event()
        events = []
        def receive(event):
            events.append(event)
            ready.set()
        task = asyncio.create_task(self.p._run_jsonl(
            [sys.executable, "-c", "import os,json,time; print(json.dumps({'pid':os.getpid()}),flush=True); time.sleep(20)"],
            stdin="", cwd=Path(tempfile.mkdtemp()), env=dict(os.environ), on_event=receive, timeout=10))
        await asyncio.wait_for(ready.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ProcessLookupError):
            os.kill(events[0]["pid"], 0)

    async def test_summary_uses_isolated_cwd_and_prompt_stdin(self):
        directory = Path(tempfile.mkdtemp())
        calls = []
        async def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            kwargs["on_event"]({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
            kwargs["on_event"]({"type": "turn.completed"})
        with patch.object(self.p, "_resolve_binary", return_value="/bin/codex"), patch.object(self.p, "_run_jsonl", side_effect=fake_run):
            self.assertEqual(await self.p.summarize("codex", "transcript content", work_dir=directory), "done")
        argv, kwargs = calls[0]
        self.assertIn("transcript content", kwargs["stdin"])
        self.assertNotIn("transcript content", " ".join(argv))
        self.assertEqual(kwargs["cwd"].parent, directory.resolve())
        self.assertNotEqual(kwargs["cwd"], directory)

    def test_status_reports_availability_without_reading_credentials(self):
        with patch.object(self.p, "_resolve_binary", side_effect=lambda name: "/bin/codex" if name == "codex" else None):
            self.assertEqual(self.p.provider_status(), {"codex": {"installed": True, "binary": "/bin/codex"}, "claude": {"installed": False, "binary": None}})

    def test_nvm_discovery_and_explicit_binary_with_reduced_path(self):
        directory = Path(tempfile.mkdtemp())
        binary = directory / 'versions/node/v22.22.1/bin/recording-test-command'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o700)
        with patch.dict(os.environ, {'NVM_DIR': str(directory), 'PATH': '/usr/bin:/bin'}):
            self.assertEqual(self.p._resolve_binary('recording-test-command'), str(binary))
        with patch.dict(os.environ, {'RECORDING_CLAUDE_BIN': str(binary)}):
            self.assertEqual(self.p._resolve_binary('claude'), str(binary.resolve()))

    async def test_success_also_reaps_detached_stdio_descendants(self):
        events = []
        source = ("import json,subprocess,sys; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
                  "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                  "print(json.dumps({'child':child.pid}),flush=True)")
        try:
            with patch.object(self.p, '_terminate_process_group', wraps=self.p._terminate_process_group) as cleanup:
                await self.p._run_jsonl([sys.executable, '-c', source], stdin='', cwd=Path(tempfile.mkdtemp()),
                                       env=dict(os.environ), on_event=events.append, timeout=3)
                self.assertEqual(cleanup.call_count, 1)
        finally:
            if events:
                try:
                    os.kill(events[0]['child'], signal.SIGTERM)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
