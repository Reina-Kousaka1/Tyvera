from __future__ import annotations

import io
import os
import signal
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.platform_runtime import (
    Architecture,
    ChildProcess,
    ChildProcessTimeout,
    OperatingSystem,
    ShutdownController,
    ShutdownDiagnostic,
    detect_host_platform,
    monotonic_seconds,
)
from tyvera.source import Diagnostic


ROOT = Path(__file__).resolve().parents[1]


class PlatformRuntimeTests(unittest.TestCase):
    def test_platform_detection_normalizes_linux_and_common_architecture_aliases(self) -> None:
        self.assertEqual(
            detect_host_platform("linux", "amd64"),
            detect_host_platform("linux", "x86_64"),
        )
        self.assertEqual(detect_host_platform("linux", "arm64").architecture, Architecture.ARM64)
        self.assertEqual(detect_host_platform("linux", "aarch64").operating_system, OperatingSystem.LINUX)
        self.assertTrue(detect_host_platform("linux", "aarch64").linux_architecture_supported)
        self.assertEqual(detect_host_platform("linux", "armv8l").architecture, Architecture.OTHER)
        self.assertFalse(detect_host_platform("linux", "i686").linux_architecture_supported)
        self.assertEqual(detect_host_platform("darwin", "arm64").operating_system, OperatingSystem.MACOS)
        self.assertEqual(detect_host_platform("win32", "AMD64").operating_system, OperatingSystem.WINDOWS)

    def test_monotonic_clock_never_moves_backwards(self) -> None:
        before = monotonic_seconds()
        after = monotonic_seconds()
        self.assertGreaterEqual(after, before)

    def test_child_process_uses_separate_arguments_and_returns_bounded_output(self) -> None:
        script = "import sys; print(sys.argv[1]); print('child-error', file=sys.stderr); sys.exit(7)"
        process = ChildProcess.start(
            sys.executable, ["-c", script, "two words && not a shell"],
            environment={"PYTHONIOENCODING": "utf-8"}, output_limit=128,
        )
        result = process.wait(timeout=10)
        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stdout.decode().strip(), "two words && not a shell")
        self.assertEqual(result.stderr.decode().strip(), "child-error")
        self.assertFalse(result.stdout_truncated)
        self.assertFalse(result.stderr_truncated)

    def test_child_process_capture_limit_does_not_stop_pipe_drain(self) -> None:
        process = ChildProcess.start(
            sys.executable,
            ["-c", "import sys; sys.stdout.write('x' * 100000); sys.stderr.write('y' * 100000)"],
            environment={}, output_limit=1024,
        )
        result = process.wait(timeout=10)
        self.assertEqual(len(result.stdout), 1024)
        self.assertEqual(len(result.stderr), 1024)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)

    def test_child_process_timeout_and_controlled_termination(self) -> None:
        process = ChildProcess.start(sys.executable, ["-c", "import time; time.sleep(30)"], environment={})
        with self.assertRaises(ChildProcessTimeout):
            process.wait(timeout=0.01)
        result = process.terminate(grace_seconds=1)
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(process.is_running)

    def test_shutdown_request_has_conventional_exit_code_and_no_value_data(self) -> None:
        controller = ShutdownController()
        controller.request(signal.SIGTERM)
        with self.assertRaises(ShutdownDiagnostic) as caught:
            controller.raise_if_requested(compile_source("fn main() {}", "shutdown.tyv")[0].functions[0].location)
        self.assertEqual(caught.exception.exit_code, 128 + signal.SIGTERM)
        self.assertIn("SIGTERM", caught.exception.message)

    def test_host_and_environment_apis_keep_values_secret_wrapped(self) -> None:
        source = '''fn main() {
    println(hostOS())
    println(hostArchitecture())
    match environment("TYVERA_PLATFORM_TEST_SECRET") {
        Some(secret) => println(secret.reveal())
        None => println("missing")
    }
}'''
        program, _ = compile_source(source, "platform.tyv")
        output = io.StringIO()
        with patch.dict(os.environ, {"TYVERA_PLATFORM_TEST_SECRET": "private-platform-value"}):
            Interpreter(program, output).run()
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], detect_host_platform().operating_system.value)
        self.assertEqual(lines[1], detect_host_platform().architecture.value)
        self.assertEqual(lines[2], "private-platform-value")

        with self.assertRaisesRegex(Diagnostic, "found Secret<String>"):
            compile_source('''fn main() {
    match environment("TYVERA_PLATFORM_TEST_SECRET") {
        Some(secret) => println(secret)
        None => println("missing")
    }
}''', "platform-secret.tyv")

    def test_signal_shutdown_unwinds_scoped_file_resource(self) -> None:
        source = '''actor Worker { on ping() {} }
fn main() {
    using file = openText("shutdown.txt", "write") {
        file.write("closed on shutdown")
        let worker = spawn(Worker())
        worker.ping()
    }
}'''
        program, _ = compile_source(source, "shutdown.tyv")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = ShutdownController()

            def diagnostics(event) -> None:
                if event.kind.value == "actor_started":
                    controller.request(signal.SIGTERM)

            interpreter = Interpreter(
                program, io.StringIO(), project_root=root,
                shutdown_controller=controller, runtime_event_sink=diagnostics,
            )
            with self.assertRaises(ShutdownDiagnostic) as caught:
                interpreter.run()
            self.assertEqual(caught.exception.exit_code, 128 + signal.SIGTERM)
            self.assertEqual((root / "shutdown.txt").read_text(encoding="utf-8"), "closed on shutdown")
            self.assertEqual(interpreter._runtime_state, "closed")

    def test_shutdown_wakes_a_task_deadline_without_sleep_polling(self) -> None:
        program, _ = compile_source('''async fn wait() { await sleepFor(seconds(60)) }
fn main() { let task = wait(); runTasks() }''', "sleep-shutdown.tyv")
        controller = ShutdownController()
        with patch("tyvera.interpreter.IOBackend") as adapter:
            backend = adapter.return_value
            backend.has_pending = False
            backend.wait.side_effect = lambda _timeout: controller.request(signal.SIGTERM)
            interpreter = Interpreter(program, io.StringIO(), shutdown_controller=controller)
            with self.assertRaises(ShutdownDiagnostic):
                interpreter.run()
            backend.wait.assert_called_once()
            self.assertGreater(backend.wait.call_args.args[0], 0)
            backend.wake.assert_called_once()
            backend.shutdown.assert_called_once()
            self.assertEqual(interpreter._runtime_state, "closed")

    def test_shutdown_before_idle_adapter_creation_cannot_lose_wakeup(self) -> None:
        program, _ = compile_source("fn main() {}", "early-shutdown.tyv")
        controller = ShutdownController()
        interpreter = Interpreter(program, io.StringIO(), shutdown_controller=controller)
        controller.request(signal.SIGINT)
        with patch("tyvera.interpreter.IOBackend") as adapter:
            with self.assertRaises(ShutdownDiagnostic):
                interpreter._wait_idle(60, program.functions[0].location)
            adapter.return_value.wait.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux process signals")
    def test_cli_signal_exit_codes_and_cleanup_for_sigint_and_sigterm(self) -> None:
        source = '''fn main() {
    using file = openText("shutdown.txt", "write") {
        file.write("closed on signal")
        println("ready")
        while true {}
    }
}'''
        for selected_signal, expected_code, expected_name in (
            (signal.SIGINT, 128 + signal.SIGINT, "SIGINT"),
            (signal.SIGTERM, 128 + signal.SIGTERM, "SIGTERM"),
        ):
            with self.subTest(signal=expected_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                script = root / "main.tyv"
                script.write_text(source, encoding="utf-8")
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(ROOT)
                environment["PYTHONUNBUFFERED"] = "1"
                process = subprocess.Popen(
                    [sys.executable, "-m", "tyvera", "run", str(script)],
                    cwd=root, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "ready")
                process.send_signal(selected_signal)
                _, error_output = process.communicate(timeout=10)
                self.assertEqual(process.returncode, expected_code)
                self.assertIn(f"runtime shutdown requested by {expected_name}", error_output)
                self.assertEqual((root / "shutdown.txt").read_text(encoding="utf-8"), "closed on signal")


if __name__ == "__main__":
    unittest.main()
