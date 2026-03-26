#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath
from queue import Empty, Queue
from typing import Any

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    FileSystemEvent = Any  # type: ignore[assignment]
    FileSystemEventHandler = object  # type: ignore[assignment]
    Observer = None
    WATCHDOG_AVAILABLE = False


class ConfigError(ValueError):
    """Raised when the configuration is invalid."""


class CommandError(RuntimeError):
    """Raised when a system command fails."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_local_path(raw_path: str | None, base_dir: Path) -> Path | None:
    if raw_path in (None, ""):
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    else:
        path = path.resolve()
    return path


def normalize_extensions(values: list[str]) -> list[str]:
    normalized = []
    for value in values:
        cleaned = value.strip().lower()
        if not cleaned:
            continue
        normalized.append(cleaned if cleaned.startswith(".") else f".{cleaned}")
    return sorted(set(normalized))


@dataclass(slots=True)
class AppConfig:
    local_watch_dir: Path
    remote_host: str
    remote_user: str
    remote_dir: str
    ssh_key_path: Path | None = None
    file_stable_wait_seconds: float = 2.0
    poll_interval_seconds: float = 5.0
    log_file: Path = Path("logs/file_watcher.log")
    state_file: Path = Path("state/transfer_state.json")
    allowed_extensions: list[str] = field(default_factory=list)
    ignore_patterns: list[str] = field(default_factory=list)
    retry_attempts: int = 3
    create_remote_dir: bool = True
    recursive: bool = False
    watch_mode: str = "auto"
    transfer_method: str = "auto"
    stable_checks_required: int = 2
    delete_after_transfer: bool = False

    @classmethod
    def load(cls, config_path: Path) -> "AppConfig":
        if not config_path.exists():
            raise ConfigError(
                f"Config file not found: {config_path}. Copy config.example.json to config.json and edit it first."
            )

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Config file is not valid JSON: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError("Config file must contain a top-level JSON object.")

        base_dir = config_path.parent.resolve()

        try:
            config = cls(
                local_watch_dir=resolve_local_path(str(raw["local_watch_dir"]), base_dir) or Path(),
                remote_host=str(raw["remote_host"]).strip(),
                remote_user=str(raw["remote_user"]).strip(),
                remote_dir=str(raw["remote_dir"]).strip(),
                ssh_key_path=resolve_local_path(raw.get("ssh_key_path"), base_dir),
                file_stable_wait_seconds=float(raw.get("file_stable_wait_seconds", 2.0)),
                poll_interval_seconds=float(raw.get("poll_interval_seconds", 5.0)),
                log_file=resolve_local_path(raw.get("log_file", "logs/file_watcher.log"), base_dir)
                or Path("logs/file_watcher.log"),
                state_file=resolve_local_path(raw.get("state_file", "state/transfer_state.json"), base_dir)
                or Path("state/transfer_state.json"),
                allowed_extensions=normalize_extensions(list(raw.get("allowed_extensions", []))),
                ignore_patterns=[str(item).strip() for item in raw.get("ignore_patterns", []) if str(item).strip()],
                retry_attempts=int(raw.get("retry_attempts", 3)),
                create_remote_dir=bool(raw.get("create_remote_dir", True)),
                recursive=bool(raw.get("recursive", False)),
                watch_mode=str(raw.get("watch_mode", "auto")).strip().lower(),
                transfer_method=str(raw.get("transfer_method", "auto")).strip().lower(),
                stable_checks_required=int(raw.get("stable_checks_required", 2)),
                delete_after_transfer=bool(raw.get("delete_after_transfer", False)),
            )
        except KeyError as exc:
            raise ConfigError(f"Missing required config field: {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid config value: {exc}") from exc

        config.validate()
        return config

    def validate(self) -> None:
        if not self.remote_host:
            raise ConfigError("remote_host must not be empty.")
        if not self.remote_user:
            raise ConfigError("remote_user must not be empty.")
        if not self.remote_dir:
            raise ConfigError("remote_dir must not be empty.")
        if self.file_stable_wait_seconds <= 0:
            raise ConfigError("file_stable_wait_seconds must be greater than 0.")
        if self.poll_interval_seconds <= 0:
            raise ConfigError("poll_interval_seconds must be greater than 0.")
        if self.retry_attempts < 1:
            raise ConfigError("retry_attempts must be at least 1.")
        if self.stable_checks_required < 2:
            raise ConfigError("stable_checks_required must be at least 2.")
        if self.watch_mode not in {"auto", "watchdog", "polling"}:
            raise ConfigError("watch_mode must be one of: auto, watchdog, polling.")
        if self.transfer_method not in {"auto", "rsync", "scp"}:
            raise ConfigError("transfer_method must be one of: auto, rsync, scp.")


def setup_logging(config: AppConfig) -> logging.Logger:
    logger = logging.getLogger("file_watcher")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(config.log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


class StateStore:
    def __init__(self, state_file: Path, logger: logging.Logger) -> None:
        self.state_file = state_file
        self.logger = logger
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"files": {}}
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return

        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.warning("State file could not be loaded (%s). Starting with an empty state.", exc)
            return

        files = payload.get("files") if isinstance(payload, dict) else None
        if isinstance(files, dict):
            self._data = {"files": files}
        else:
            self.logger.warning("State file format was invalid. Starting with an empty state.")

    def _write(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_file.with_name(f"{self.state_file.name}.tmp")
        tmp_path.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.state_file)

    def was_successful(self, path: Path, file_size: int, file_mtime_ns: int) -> bool:
        with self._lock:
            entry = self._data["files"].get(str(path))
        if not isinstance(entry, dict):
            return False
        return (
            entry.get("status") == "success"
            and entry.get("size") == file_size
            and entry.get("mtime_ns") == file_mtime_ns
        )

    def mark_success(self, path: Path, file_size: int, file_mtime_ns: int, local_deleted: bool = False) -> None:
        with self._lock:
            self._data["files"][str(path)] = {
                "status": "success",
                "size": file_size,
                "mtime_ns": file_mtime_ns,
                "last_attempt_at": utc_now(),
                "last_success_at": utc_now(),
                "local_deleted": local_deleted,
                "last_local_delete_at": utc_now() if local_deleted else None,
                "last_error": None,
            }
            self._write()

    def mark_failure(self, path: Path, file_size: int, file_mtime_ns: int, error: str) -> None:
        with self._lock:
            self._data["files"][str(path)] = {
                "status": "failed",
                "size": file_size,
                "mtime_ns": file_mtime_ns,
                "last_attempt_at": utc_now(),
                "last_success_at": None,
                "last_error": error,
            }
            self._write()

    def flush(self) -> None:
        with self._lock:
            self._write()


class TransferClient:
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self._ssh_target = f"{self.config.remote_user}@{self.config.remote_host}"
        self.transfer_method = ""

    def prepare(self) -> None:
        if shutil.which("ssh") is None:
            raise ConfigError("ssh is not installed on this machine.")

        self._run_remote_command("printf ready", timeout=15)
        self.logger.info("SSH connectivity validated for %s", self._ssh_target)

        if self.config.create_remote_dir:
            self._run_remote_command(f"mkdir -p -- {shlex.quote(self.config.remote_dir)}", timeout=15)
            self.logger.info("Remote directory is ready: %s", self.config.remote_dir)
        else:
            self._run_remote_command(f"test -d {shlex.quote(self.config.remote_dir)}", timeout=15)
            self.logger.info("Remote directory exists: %s", self.config.remote_dir)

        self.transfer_method = self._select_transfer_method()
        self.logger.info("Using transfer method: %s", self.transfer_method)

    def _select_transfer_method(self) -> str:
        wants = self.config.transfer_method
        local_rsync = shutil.which("rsync") is not None
        local_scp = shutil.which("scp") is not None
        remote_rsync = local_rsync and self._remote_command_exists("rsync")

        if wants == "rsync":
            if not local_rsync:
                raise ConfigError("transfer_method is rsync, but rsync is not installed locally.")
            if not remote_rsync:
                raise ConfigError("transfer_method is rsync, but rsync is not available on the remote server.")
            return "rsync"

        if wants == "scp":
            if not local_scp:
                raise ConfigError("transfer_method is scp, but scp is not installed locally.")
            return "scp"

        if local_rsync and remote_rsync:
            return "rsync"

        if local_scp:
            if local_rsync and not remote_rsync:
                self.logger.warning("Remote rsync is unavailable; falling back to scp.")
            return "scp"

        raise ConfigError("No supported transfer command is available locally. Install rsync or scp.")

    def _ssh_base_args(self) -> list[str]:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if self.config.ssh_key_path:
            args.extend(["-i", str(self.config.ssh_key_path)])
        return args

    def _scp_base_args(self) -> list[str]:
        args = ["scp", "-p", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if self.config.ssh_key_path:
            args.extend(["-i", str(self.config.ssh_key_path)])
        return args

    def _remote_command_exists(self, command: str) -> bool:
        try:
            self._run_remote_command(f"command -v {shlex.quote(command)} >/dev/null 2>&1", timeout=15)
            return True
        except CommandError:
            return False

    def _run_remote_command(self, command: str, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        args = [*self._ssh_base_args(), self._ssh_target, command]
        return self._run_command(args, timeout=timeout)

    def _run_command(
        self, args: list[str], timeout: int | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"Command timed out: {' '.join(args)}") from exc

        if check and completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or f"Command failed: {' '.join(args)}"
            raise CommandError(message)
        return completed

    def _rsync_ssh_command(self) -> str:
        return " ".join(shlex.quote(arg) for arg in self._ssh_base_args())

    def _remote_spec(self, remote_path: str) -> str:
        return f"{self._ssh_target}:{shlex.quote(remote_path)}"

    def _relative_destination(self, local_path: Path) -> Path:
        if self.config.recursive:
            return local_path.resolve().relative_to(self.config.local_watch_dir)
        return Path(local_path.name)

    def transfer(self, local_path: Path) -> None:
        relative_destination = self._relative_destination(local_path)
        if self.transfer_method == "rsync":
            self._transfer_with_rsync(local_path, relative_destination)
            return
        self._transfer_with_scp(local_path, relative_destination)

    def _transfer_with_rsync(self, local_path: Path, relative_destination: Path) -> None:
        destination_root = str(PurePosixPath(self.config.remote_dir))
        if not destination_root.endswith("/"):
            destination_root = f"{destination_root}/"

        if self.config.recursive:
            source = f"{self.config.local_watch_dir.as_posix()}/./{relative_destination.as_posix()}"
        else:
            source = str(local_path)

        args = [
            "rsync",
            "-az",
            "--partial",
            "-e",
            self._rsync_ssh_command(),
            source,
            self._remote_spec(destination_root),
        ]
        self._run_command(args)

    def _transfer_with_scp(self, local_path: Path, relative_destination: Path) -> None:
        remote_destination = PurePosixPath(self.config.remote_dir) / PurePosixPath(relative_destination.as_posix())
        remote_parent = str(remote_destination.parent)
        if remote_parent:
            self._run_remote_command(f"mkdir -p -- {shlex.quote(remote_parent)}", timeout=15)

        args = [*self._scp_base_args(), str(local_path), self._remote_spec(str(remote_destination))]
        self._run_command(args)


class QueueingEventHandler(FileSystemEventHandler):
    def __init__(self, service: "FileWatcherService") -> None:
        self.service = service

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.service.enqueue_file(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.service.enqueue_file(Path(event.dest_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.service.enqueue_file(Path(event.src_path))


class FileWatcherService:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        state_store: StateStore,
        transfer_client: TransferClient,
    ) -> None:
        self.config = config
        self.logger = logger
        self.state_store = state_store
        self.transfer_client = transfer_client
        self.stop_event = threading.Event()
        self.queue: Queue[Path] = Queue()
        self._pending_paths: set[str] = set()
        self._pending_lock = threading.Lock()
        self._worker_thread = threading.Thread(target=self._worker_loop, name="file-transfer-worker", daemon=True)
        self._observer: Observer | None = None

    @property
    def use_watchdog(self) -> bool:
        if self.config.watch_mode == "polling":
            return False
        if self.config.watch_mode == "watchdog" and not WATCHDOG_AVAILABLE:
            raise ConfigError("watch_mode is watchdog, but the watchdog package is not installed.")
        return WATCHDOG_AVAILABLE

    def request_stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        use_watchdog = self.use_watchdog
        self._worker_thread.start()
        self.logger.info(
            "Watching %s (mode=%s, recursive=%s)",
            self.config.local_watch_dir,
            "watchdog" if use_watchdog else "polling",
            self.config.recursive,
        )

        try:
            if use_watchdog:
                self._run_with_watchdog()
            else:
                self._run_with_polling()
        finally:
            self.stop_event.set()
            if self._observer is not None:
                self._observer.stop()
                self._observer.join()
                self._observer = None

            self.queue.join()
            self._worker_thread.join()
            self.state_store.flush()
            self.logger.info("Service shutdown complete.")

    def _run_with_watchdog(self) -> None:
        self._observer = Observer()
        self._observer.schedule(
            QueueingEventHandler(self),
            str(self.config.local_watch_dir),
            recursive=self.config.recursive,
        )
        self._observer.start()
        self.logger.info("Filesystem observer started.")

        self.scan_existing_files()
        while not self.stop_event.wait(self.config.poll_interval_seconds):
            self.scan_existing_files()

    def _run_with_polling(self) -> None:
        self.logger.info("Polling loop started.")
        self.scan_existing_files()
        while not self.stop_event.wait(self.config.poll_interval_seconds):
            self.scan_existing_files()

    def _iter_watch_files(self) -> list[Path]:
        if self.config.recursive:
            iterator = self.config.local_watch_dir.rglob("*")
        else:
            iterator = self.config.local_watch_dir.iterdir()

        files = []
        for path in iterator:
            try:
                if path.is_file() and self._matches_filters(path):
                    files.append(path.resolve())
            except OSError:
                continue
        return files

    def scan_existing_files(self) -> None:
        for path in self._iter_watch_files():
            if self.stop_event.is_set():
                return

            try:
                stat_result = path.stat()
            except OSError:
                continue

            if self.state_store.was_successful(path, stat_result.st_size, stat_result.st_mtime_ns):
                continue

            self.enqueue_file(path)

    def enqueue_file(self, path: Path) -> None:
        resolved_path = path.expanduser().resolve()
        if not self._matches_filters(resolved_path):
            return

        path_key = str(resolved_path)
        with self._pending_lock:
            if path_key in self._pending_paths:
                return
            self._pending_paths.add(path_key)

        self.queue.put(resolved_path)
        self.logger.info("Queued file for transfer: %s", resolved_path)

    def _matches_filters(self, path: Path) -> bool:
        if path.is_dir():
            return False

        if self.config.allowed_extensions and path.suffix.lower() not in self.config.allowed_extensions:
            return False

        full_path = str(path)
        for pattern in self.config.ignore_patterns:
            if fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(full_path, pattern):
                return False

        return True

    def _worker_loop(self) -> None:
        while True:
            if self.stop_event.is_set() and self.queue.empty():
                return

            try:
                path = self.queue.get(timeout=1)
            except Empty:
                continue

            try:
                self._process_file(path)
            except Exception:
                self.logger.exception("Unexpected error while processing %s", path)
            finally:
                with self._pending_lock:
                    self._pending_paths.discard(str(path))
                self.queue.task_done()

    def _process_file(self, path: Path) -> None:
        if not self._matches_filters(path):
            return

        stable_stat = self._wait_for_stable_file(path)
        if stable_stat is None:
            return

        if self.state_store.was_successful(path, stable_stat.st_size, stable_stat.st_mtime_ns):
            self.logger.info("Skipping already-transferred file: %s", path)
            return

        last_error: str | None = None
        for attempt in range(1, self.config.retry_attempts + 1):
            if self.stop_event.is_set():
                return

            try:
                self.transfer_client.transfer(path)
                local_deleted = self._delete_local_file_if_requested(path, stable_stat)
                self.state_store.mark_success(
                    path,
                    stable_stat.st_size,
                    stable_stat.st_mtime_ns,
                    local_deleted=local_deleted,
                )
                self.logger.info("Transfer succeeded: %s", path)
                return
            except (CommandError, OSError, ValueError) as exc:
                last_error = str(exc)
                if attempt >= self.config.retry_attempts:
                    break

                backoff_seconds = self._retry_backoff_seconds(attempt)
                self.logger.warning(
                    "Transfer failed for %s on attempt %s/%s: %s. Retrying in %ss.",
                    path,
                    attempt,
                    self.config.retry_attempts,
                    last_error,
                    backoff_seconds,
                )
                if self.stop_event.wait(backoff_seconds):
                    return

        if last_error is None:
            last_error = "Transfer failed for an unknown reason."
        self.state_store.mark_failure(path, stable_stat.st_size, stable_stat.st_mtime_ns, last_error)
        self.logger.error("Transfer failed after %s attempts for %s: %s", self.config.retry_attempts, path, last_error)

    def _wait_for_stable_file(self, path: Path) -> os.stat_result | None:
        stable_reads = 0
        last_signature: tuple[int, int] | None = None

        while not self.stop_event.is_set():
            if self.stop_event.wait(self.config.file_stable_wait_seconds):
                return None

            try:
                stat_result = path.stat()
            except FileNotFoundError:
                self.logger.warning("File disappeared before transfer: %s", path)
                return None
            except OSError as exc:
                self.logger.warning("Could not read file state for %s: %s", path, exc)
                return None

            if not path.is_file():
                return None

            signature = (stat_result.st_size, stat_result.st_mtime_ns)
            if signature == last_signature:
                stable_reads += 1
            else:
                last_signature = signature
                stable_reads = 1
                self.logger.info("Waiting for file to stabilize: %s", path)

            if stable_reads >= self.config.stable_checks_required:
                return stat_result

        return None

    def _delete_local_file_if_requested(self, path: Path, expected_stat: os.stat_result) -> bool:
        if not self.config.delete_after_transfer:
            return False

        expected_signature = (expected_stat.st_size, expected_stat.st_mtime_ns)
        try:
            current_stat = path.stat()
        except FileNotFoundError:
            self.logger.info("Local file was already removed after transfer: %s", path)
            return True
        except OSError as exc:
            self.logger.error("Transfer succeeded but local delete pre-check failed for %s: %s", path, exc)
            return False

        current_signature = (current_stat.st_size, current_stat.st_mtime_ns)
        if current_signature != expected_signature:
            self.logger.warning(
                "Local file changed after transfer, so auto-delete was skipped for %s.",
                path,
            )
            return False

        try:
            path.unlink()
        except FileNotFoundError:
            self.logger.info("Local file was already removed after transfer: %s", path)
            return True
        except OSError as exc:
            self.logger.error("Transfer succeeded but failed to delete local file %s: %s", path, exc)
            return False

        self.logger.info("Deleted local file after successful transfer: %s", path)
        return True

    @staticmethod
    def _retry_backoff_seconds(attempt_number: int) -> int:
        return min(5 * (3 ** (attempt_number - 1)), 30)


def install_signal_handlers(service: FileWatcherService, logger: logging.Logger) -> None:
    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s, shutting down.", signum)
        service.request_stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def validate_startup(config: AppConfig, logger: logging.Logger, transfer_client: TransferClient) -> None:
    if not config.local_watch_dir.exists():
        raise ConfigError(f"Local watch directory does not exist: {config.local_watch_dir}")
    if not config.local_watch_dir.is_dir():
        raise ConfigError(f"Local watch directory is not a directory: {config.local_watch_dir}")
    if config.ssh_key_path and not config.ssh_key_path.exists():
        raise ConfigError(f"SSH key path does not exist: {config.ssh_key_path}")

    logger.info("Configuration loaded successfully.")
    transfer_client.prepare()


def parse_args() -> argparse.Namespace:
    default_config = Path(__file__).with_name("config.json")
    parser = argparse.ArgumentParser(description="Watch a local directory and transfer new files to a remote server.")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config,
        help=f"Path to the JSON config file (default: {default_config})",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate config, SSH connectivity, and remote setup, then exit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = AppConfig.load(args.config.resolve())
        logger = setup_logging(config)
        state_store = StateStore(config.state_file, logger)
        transfer_client = TransferClient(config, logger)
        validate_startup(config, logger, transfer_client)

        if args.validate_only:
            logger.info("Validation completed successfully.")
            return 0

        service = FileWatcherService(config, logger, state_store, transfer_client)
        install_signal_handlers(service, logger)
        service.run()
        return 0
    except (ConfigError, CommandError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
