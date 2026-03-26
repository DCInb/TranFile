# Auto File Sender

This project watches a local directory and automatically transfers newly detected files to another Ubuntu server over SSH.

## What it does

- Watches a local directory continuously.
- Waits for files to stop changing before transfer.
- Sends files with `rsync` when available, or `scp` as a fallback.
- Tracks successful transfers in a local JSON state file so restarts do not resend the same file version.
- Retries failed transfers with backoff and writes logs to both stdout and a rotating log file.

## Files

- `file_watcher.py` - main service
- `config.example.json` - example configuration
- `requirements.txt` - Python dependency list

## Setup

1. Install Python dependencies:

```bash
pip install -r requirements.txt
```

2. Copy the example config and edit it:

```bash
cp config.example.json config.json
```

3. Make sure SSH key-based login works from the local machine to the remote server:

```bash
ssh remote_user@remote_host
```

4. Validate the configuration and remote connectivity:

```bash
python3 file_watcher.py --validate-only
```

5. Start the service:

```bash
python3 file_watcher.py
```

## Config fields

- `local_watch_dir`: local directory to watch
- `remote_host`: remote server IP or hostname
- `remote_user`: SSH user on the remote server
- `remote_dir`: remote destination directory
- `ssh_key_path`: optional SSH key path
- `file_stable_wait_seconds`: wait between file stability checks
- `poll_interval_seconds`: scan interval, and retry rescan interval
- `log_file`: path to the rotating log file
- `state_file`: JSON file that stores transfer history
- `allowed_extensions`: optional extension allowlist, for example `[".dat", ".bin"]`
- `ignore_patterns`: optional glob patterns to skip
- `retry_attempts`: number of transfer attempts before marking a failure
- `create_remote_dir`: create `remote_dir` automatically on startup
- `recursive`: include subdirectories under `local_watch_dir`
- `watch_mode`: `auto`, `watchdog`, or `polling`
- `transfer_method`: `auto`, `rsync`, or `scp`
- `stable_checks_required`: number of unchanged checks required before transfer
- `delete_after_transfer`: delete the local file after a successful transfer

## Notes

- In `auto` mode the service uses `watchdog` when the package is installed, otherwise it falls back to polling.
- In `auto` transfer mode the service prefers `rsync` and falls back to `scp` if needed.
- If a file fails all retries, it stays unconfirmed in the state file and will be retried on later scans.
- If `delete_after_transfer` is enabled, deletion only happens after a successful send. If the local file changes again before deletion, it is kept locally and can be transferred again.
- The "Plan.md" is the file for codex to create the program.
