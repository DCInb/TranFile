import logging
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from file_watcher import (
    AppConfig,
    ConfigError,
    FileWatcherService,
    InstanceLock,
    TransferClient,
)


class RecordingTransferClient(TransferClient):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config, logging.getLogger("test_transfer_paths"))
        self.commands: list[list[str]] = []
        self.timeouts: list[int | None] = []

    def _run_command(
        self, args: list[str], timeout: int | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        self.timeouts.append(timeout)
        return subprocess.CompletedProcess(args, 0, "", "")


class RecordingStateStore:
    def __init__(self) -> None:
        self.successes: list[Path] = []

    def was_successful(self, path: Path, file_size: int, file_mtime_ns: int) -> bool:
        return False

    def mark_success(
        self,
        path: Path,
        file_size: int,
        file_mtime_ns: int,
        local_deleted: bool = False,
    ) -> None:
        self.successes.append(path)

    def mark_failure(
        self, path: Path, file_size: int, file_mtime_ns: int, error: str
    ) -> None:
        raise AssertionError(f"unexpected transfer failure for {path}: {error}")


class EventTransferClient:
    def __init__(self) -> None:
        self.transfers: list[Path] = []
        self.transferred = threading.Event()

    def transfer(self, path: Path) -> None:
        self.transfers.append(path)
        self.transferred.set()


class ImmediateEvent:
    """Deterministic Event stand-in that never sleeps or requests shutdown."""

    def __init__(self) -> None:
        self.wait_calls = 0

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls += 1
        return False


class ScriptedStatPath:
    def __init__(self, signatures: list[tuple[int, int]]) -> None:
        self.signatures = iter(signatures)
        self.stat_calls = 0

    def stat(self) -> os.stat_result:
        self.stat_calls += 1
        size, mtime_ns = next(self.signatures)
        return SimpleNamespace(st_size=size, st_mtime_ns=mtime_ns)  # type: ignore[return-value]

    def is_file(self) -> bool:
        return True

    def __str__(self) -> str:
        return "scripted-changing-file"


class DeferringFileWatcherService(FileWatcherService):
    """Exercise worker scheduling without wall-clock stability sleeps."""

    def __init__(self, *args: object, changing: Path, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.changing = changing.resolve()
        self.wait_order: list[str] = []
        self.changing_attempts = 0
        self.changing_retried = threading.Event()

    def _wait_for_stable_file(self, path: Path) -> os.stat_result | None:
        self.wait_order.append(path.name)
        if path == self.changing:
            self.changing_attempts += 1
            if self.changing_attempts >= 2:
                self.changing_retried.set()
            return None
        return path.stat()


class TransferPathTests(unittest.TestCase):
    def _config(self, watch_dir: Path, *, recursive: bool, transfer_method: str) -> AppConfig:
        return AppConfig(
            local_watch_dir=watch_dir,
            remote_host="example.test",
            remote_user="ubuntu",
            remote_dir="/srv/incoming",
            recursive=recursive,
            transfer_method=transfer_method,
            rsync_compress=False,
        )

    def test_rsync_preserves_relative_directory_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            watch_dir = Path(temp_dir).resolve()
            local_path = watch_dir / "level1" / "level2" / "output.bin"
            client = RecordingTransferClient(
                self._config(watch_dir, recursive=True, transfer_method="rsync")
            )
            client.transfer_method = "rsync"

            client.transfer(local_path)

        rsync_command = client.commands[0]
        self.assertIn("--relative", rsync_command)
        self.assertIn("-a", rsync_command)
        self.assertNotIn("-z", rsync_command)
        self.assertIn("--partial-dir=.tranfile-partial", rsync_command)
        self.assertIn("--timeout=300", rsync_command)
        self.assertIn(f"{watch_dir.as_posix()}/./level1/level2/output.bin", rsync_command)
        self.assertEqual("ubuntu@example.test:/srv/incoming/", rsync_command[-1])
        self.assertEqual(7200, client.timeouts[0])

    def test_scp_preserves_relative_directory_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            watch_dir = Path(temp_dir).resolve()
            local_path = watch_dir / "level1" / "level2" / "output.bin"
            client = RecordingTransferClient(
                self._config(watch_dir, recursive=True, transfer_method="scp")
            )
            client.transfer_method = "scp"

            client.transfer(local_path)

        mkdir_command = client.commands[0]
        scp_command = client.commands[1]
        self.assertEqual("ssh", mkdir_command[0])
        self.assertEqual("mkdir -p -- /srv/incoming/level1/level2", mkdir_command[-1])
        self.assertEqual("scp", scp_command[0])
        self.assertEqual(
            "ubuntu@example.test:/srv/incoming/level1/level2/output.bin",
            scp_command[-1],
        )
        self.assertEqual(7200, client.timeouts[-1])

    def test_instance_lock_rejects_a_second_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.json"
            config.write_text("{}\n", encoding="utf-8")
            lock = root / "watcher.lock"
            with InstanceLock(lock, config):
                with self.assertRaises(ConfigError):
                    with InstanceLock(lock, config):
                        pass

    def test_stability_attempt_yields_after_first_observed_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            watch_dir = Path(temp_dir).resolve()
            config = self._config(watch_dir, recursive=True, transfer_method="rsync")
            config.stable_checks_required = 4
            service = FileWatcherService(
                config,
                logging.getLogger("test_stability_yield"),
                RecordingStateStore(),  # type: ignore[arg-type]
                EventTransferClient(),  # type: ignore[arg-type]
            )
            immediate = ImmediateEvent()
            service.stop_event = immediate  # type: ignore[assignment]
            path = ScriptedStatPath([(10, 100), (11, 101)])

            result = service._wait_for_stable_file(path)  # type: ignore[arg-type]

        self.assertIsNone(result)
        self.assertEqual(2, path.stat_calls)
        self.assertEqual(2, immediate.wait_calls)

    def test_perpetually_changing_first_file_does_not_starve_stable_second(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            watch_dir = Path(temp_dir).resolve()
            changing = watch_dir / "00-changing.log"
            stable = watch_dir / "01-stable.bin"
            changing.write_text("changing\n", encoding="utf-8")
            stable.write_bytes(b"stable")
            config = self._config(watch_dir, recursive=True, transfer_method="rsync")
            state = RecordingStateStore()
            client = EventTransferClient()
            service = DeferringFileWatcherService(
                config,
                logging.getLogger("test_worker_fairness"),
                state,  # type: ignore[arg-type]
                client,  # type: ignore[arg-type]
                changing=changing,
            )

            service.enqueue_file(changing)
            service.enqueue_file(stable)
            service._worker_thread.start()
            self.assertTrue(client.transferred.wait(timeout=2.0))
            self.assertTrue(service.changing_retried.wait(timeout=2.0))
            service.request_stop()
            service.queue.join()
            service._worker_thread.join(timeout=2.0)

        self.assertFalse(service._worker_thread.is_alive())
        self.assertEqual([stable], client.transfers)
        self.assertEqual([stable], state.successes)
        self.assertGreaterEqual(len(service.wait_order), 3)
        self.assertEqual(
            [changing.name, stable.name, changing.name], service.wait_order[:3]
        )


if __name__ == "__main__":
    unittest.main()
