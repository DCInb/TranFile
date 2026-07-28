import logging
import subprocess
import tempfile
import unittest
from pathlib import Path

from file_watcher import AppConfig, ConfigError, InstanceLock, TransferClient


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


if __name__ == "__main__":
    unittest.main()
