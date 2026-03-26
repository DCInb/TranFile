# Plan: Local File Watcher and Auto-Transfer Service

## Goal
Build a small long-running Ubuntu program that watches a local directory for newly generated files and automatically sends each new file to a specified directory on another Ubuntu server in the same local network.

The program should:
1. Monitor a given local directory continuously.
2. Detect newly created files.
3. Transfer each new file to a given remote directory on another server by local IP.
4. Return to waiting for the next new file.
5. Keep running reliably with useful logs and simple configuration.

---

## Recommended Approach

### Preferred transfer method
Use **SSH + `rsync`** (or `scp` as fallback).

Why:
- Works well between Ubuntu servers.
- Easy to automate with SSH keys.
- `rsync` is efficient and robust.
- Can preserve timestamps and resume partially transferred files better than plain copy methods.

### Preferred file monitoring method
Use Python with one of these approaches:
- **Recommended:** `watchdog` library for filesystem events.
- **Fallback:** polling loop that scans the directory every few seconds.

Why:
- `watchdog` reacts quickly to new files.
- Polling is simpler but less efficient.

---

## Functional Requirements

### 1. Configuration
The program should accept configuration values such as:
- `local_watch_dir`
- `remote_host` (local IP address)
- `remote_user`
- `remote_dir`
- `ssh_key_path` (optional if default key is used)
- `file_stable_wait_seconds` (time to wait before transfer, to avoid copying incomplete files)
- `poll_interval_seconds` (only for polling fallback)
- `log_file`
- `allowed_extensions` (optional)
- `ignore_patterns` (optional)

Config can be stored in:
- `.env`
- JSON file
- YAML file
- command-line arguments

**Recommendation:** use a simple `.env` or JSON config first.

---

## Core Behavior

### 2. Startup behavior
On startup, the program should:
- Validate that the local watch directory exists.
- Validate SSH connectivity to the remote server.
- Validate that the remote directory exists, or create it if allowed.
- Initialize logging.
- Start the monitoring loop.

### 3. Detecting new files
The program should detect when a new file appears in the watched local directory.

Important details:
- Ignore directories unless recursive support is explicitly required.
- Only process files, not temporary partial files if possible.
- Avoid duplicate sends if the process restarts.

### 4. Ensure file is complete before transfer
A file may appear before writing is finished.

Before transfer, the program should:
- Wait briefly after detection.
- Check that file size is stable across 2 or more checks.
- Only send after the file is no longer changing.

This avoids copying incomplete output files.

### 5. Transfer file
Once stable, send the file to the remote directory using:
```bash
rsync -avz /local/path/file remote_user@remote_host:/remote/dir/
```

Possible Python execution methods:
- `subprocess.run(...)`
- a small wrapper function around `rsync`

### 6. After successful transfer
After a successful transfer:
- Write a success log entry.
- Mark the file as already processed.
- Return to the monitoring loop.

### 7. On transfer failure
If transfer fails:
- Log the error.
- Retry a limited number of times with backoff.
- If retries fail, leave the file unmarked so it can be retried later.

---

## Suggested Implementation Structure

### Option A: Single Python script
Good first version.

Suggested files:
- `file_watcher.py` — main program
- `config.json` or `.env` — configuration
- `requirements.txt` — dependencies
- `README.md` — setup and usage

### Option B: Small package structure
Better if code may grow.

Suggested layout:
```text
auto_file_sender/
  main.py
  watcher.py
  transfer.py
  config.py
  state.py
  logger.py
requirements.txt
README.md
```

**Recommendation:** start with Option A.

---

## State Tracking

To avoid sending the same file multiple times, maintain a local state store.

Possible approaches:
- SQLite database
- JSON state file
- in-memory set only (not recommended because restart loses history)

**Recommendation:** use a JSON file or SQLite.

Track fields like:
- absolute file path
- file size
- last modified time
- transfer status
- last transfer attempt time

---

## Main Loop Design

### Event-driven version (`watchdog`)
1. Start observer on watch directory.
2. On file-created event:
   - verify it is a file
   - wait until file is stable
   - transfer file
   - record success/failure
3. Continue waiting for next event.

### Polling version
1. Scan directory contents.
2. Compare against known processed files.
3. For each unseen file:
   - verify stable
   - transfer
   - record success/failure
4. Sleep for configured interval.
5. Repeat forever.

---

## Reliability Features

### Logging
Add logs for:
- service startup
- config loaded
- remote connectivity success/failure
- file detected
- file stable check status
- transfer success
- transfer retry
- transfer final failure

### Retry behavior
Use simple retry logic:
- 3 attempts
- exponential backoff, e.g. 5s, 15s, 30s

### Restart behavior
On restart:
- reload processed-state database/file
- optionally scan for unsent files already present in the watch directory

### Graceful shutdown
Handle signals like:
- `SIGINT`
- `SIGTERM`

On shutdown:
- flush state to disk
- stop observer cleanly
- write shutdown log entry

---

## Security Setup

### SSH key-based authentication
Set up passwordless SSH from source server to destination server:

```bash
ssh-keygen -t ed25519
ssh-copy-id remote_user@REMOTE_IP
```

Then test:
```bash
ssh remote_user@REMOTE_IP
```

This is strongly preferred over password-based automation.

---

## Deployment Plan

### Phase 1: Prototype
Build a simple Python script that:
- watches one directory
- sends new stable files with `rsync`
- writes logs to console

### Phase 2: Reliability
Add:
- persistent processed-file tracking
- retries
- config file
- file-stability checks

### Phase 3: Productionize
Add:
- systemd service
- rotating log file
- health checks / better error reporting
- optional recursive directory support

---

## systemd Service Plan
Create a systemd unit so the watcher runs automatically after reboot.

Example goals:
- start on boot
- restart automatically on failure
- run in background as a service user

Suggested unit outline:
```ini
[Unit]
Description=Auto file sender service
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/python3 /path/to/project/file_watcher.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Edge Cases to Handle

The program should consider:
- file appears but is still being written
- network temporarily unavailable
- remote host unreachable
- SSH authentication failure
- remote directory missing
- same filename generated twice
- very large files
- source file deleted before transfer begins
- temporary files such as `.tmp`, `.part`, editor swap files

---

## Technical Decisions for Codex

Ask Codex to implement with:
- Python 3.10+
- `watchdog` for monitoring
- `subprocess` calling `rsync`
- JSON state file for processed history
- standard `logging` module
- graceful shutdown support
- optional `.env` configuration using `python-dotenv`

---

## Minimal Acceptance Criteria

The solution is complete when:
1. A user can configure local source directory and remote destination.
2. The program runs continuously on Ubuntu.
3. A newly created file in the local directory is detected automatically.
4. The program waits until the file is stable.
5. The file is transferred successfully to the remote Ubuntu server.
6. The program logs the result.
7. The program returns to waiting for the next file.
8. Restarting the program does not cause already-sent files to be resent unnecessarily.

---

## Concrete Build Tasks for Codex

1. Create a Python project for Ubuntu.
2. Add configuration loading from `.env` or JSON.
3. Implement directory watching with `watchdog`.
4. Add logic to ignore directories and temporary files.
5. Add file stability check before transfer.
6. Implement remote transfer with `rsync` over SSH.
7. Add retry logic for failed transfers.
8. Add persistent processed-file tracking using JSON or SQLite.
9. Add structured logging.
10. Add graceful shutdown handlers.
11. Add a `README.md` with setup instructions.
12. Add a sample `systemd` service file.

---

## Suggested Prompt for Codex

Use this prompt with Codex:

> Build a production-ready Python service for Ubuntu that monitors a local directory for newly created files and automatically transfers each completed file to a remote directory on another Ubuntu server in the same LAN using `rsync` over SSH. The service should use `watchdog` for filesystem monitoring, wait until files are stable before transferring, retry failed transfers, persist processed-file state to avoid duplicate sends across restarts, log all major events, load configuration from `.env` or JSON, and include a `systemd` unit file plus README setup instructions.

---

## Nice-to-Have Enhancements
Optional future improvements:
- recursive subdirectory watching
- checksum verification after transfer
- move or delete source file after confirmed send
- remote directory auto-creation
- Prometheus metrics
- email or Slack alerts on repeated failures
- file extension filters
- max concurrent transfers

---

## Final Recommendation
For the first implementation, use:
- Python
- `watchdog`
- `rsync` over SSH
- JSON state file
- `systemd`

This will be simple, reliable, and easy for Codex to build and maintain.
