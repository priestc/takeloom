"""Remote backup & collaboration sync via rsync over SSH."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

import click


def _remote_path(remote: str, project_name: str) -> str:
    """Join remote base and project name, handling host:path format.

    Backslash-escapes spaces in the path portion (never the host). rsync
    forwards this argument to the remote shell over ssh, and the rsync
    client bundled with macOS (an openrsync build that reports itself as
    "rsync version 2.6.9 compatible" and doesn't support --protect-args)
    does not shell-quote it — an unescaped space gets split into
    separate arguments on the remote end, silently truncating the
    destination path (e.g. ".../Takeloom Studio Vault/sessions/..."
    becomes just ".../Takeloom", with everything after the space lost —
    this is exactly what happened before this fix, more than once)."""
    if ":" in remote:
        host, path = remote.split(":", 1)
        joined = os.path.join(path, project_name)
        return f"{host}:{joined.replace(' ', chr(92) + ' ')}"
    return os.path.join(remote, project_name)


def _ensure_remote_dir(remote: str, project_name: str) -> None:
    """mkdir -p the destination directory on the remote host before an
    upload. This rsync client doesn't support --mkpath and won't create
    more than one missing intermediate directory level on its own, so a
    nested vault destination (e.g. "sessions/Album1/2026-.../") fails
    outright unless something else creates "sessions/Album1/" first.
    Best-effort: if this fails, the rsync call right after it will
    surface the real error."""
    if ":" not in remote:
        return
    host, path = remote.split(":", 1)
    remote_dir = os.path.join(path, project_name)
    try:
        subprocess.run(
            ["ssh", host, f"mkdir -p {shlex.quote(remote_dir)}"],
            capture_output=True, timeout=15,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass


def sync_down(project_path: Path, remote: str) -> None:
    """Download updates from the remote backup server."""
    project_name = project_path.name
    source = _remote_path(remote, project_name) + "/"
    dest = f"{project_path}/"
    cmd = ["rsync", "-avz", "--checksum", source, dest]
    click.echo(f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            click.echo(f"Warning: sync down failed (exit {result.returncode})")
        else:
            click.echo("Sync down complete.")
    except FileNotFoundError:
        click.echo("Warning: rsync not found. Skipping sync.")
    except Exception as e:
        click.echo(f"Warning: sync down error: {e}")


def sync_up(project_path: Path, remote: str) -> None:
    """Upload project to the remote backup server."""
    project_name = project_path.name
    source = f"{project_path}/"
    _ensure_remote_dir(remote, project_name)
    dest = _remote_path(remote, project_name) + "/"
    cmd = ["rsync", "-avz", "--checksum", source, dest]
    click.echo(f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            click.echo(f"Warning: sync up failed (exit {result.returncode})")
        else:
            click.echo("Sync up complete.")
    except FileNotFoundError:
        click.echo("Warning: rsync not found. Skipping sync.")
    except Exception as e:
        click.echo(f"Warning: sync up error: {e}")


def sync_vault_session_up(local_dir: Path, vault_relative: str, remote: str) -> bool:
    """Upload one Studio Session Vault session's directory to the remote
    backup server, preserving its path relative to the vault root (e.g.
    "My Album/2026-08-05_14-30-00_bass") rather than just its basename —
    unlike sync_up/sync_down, which are keyed by a project's own bare
    directory name. Returns whether it succeeded rather than only
    click.echoing a warning: the vault's "remote" mode (see
    takeloom/vault.py) needs a real success signal to decide whether it's
    safe to delete the local copy afterward, and this is called from
    backend.py's background processing thread — not a CLI context — so it
    reports through its own return value instead of stdout."""
    _ensure_remote_dir(remote, vault_relative)
    dest = _remote_path(remote, vault_relative) + "/"
    cmd = ["rsync", "-avz", "--checksum", f"{local_dir}/", dest]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def sync_vault_file_down(remote: str, vault_relative: str, local_path: Path) -> bool:
    """Download one file from the remote backup server into the vault,
    at `vault_relative`'s path relative to the vault root (e.g.
    "backing_tracks/a1b2c3d4_song1.mp3") — used by
    vault.ensure_setlist_files_local to pull back a file "remote" vault
    mode already pruned locally. Returns whether it succeeded (and the
    file now actually exists locally), same success-signal reasoning as
    sync_vault_session_up."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    source = _remote_path(remote, vault_relative)
    cmd = ["rsync", "-avz", "--checksum", source, str(local_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0 and local_path.exists()
    except (FileNotFoundError, OSError):
        return False


def sync_vault_file_up(local_path: Path, vault_relative: str, remote: str) -> bool:
    """Upload one local file to `vault_relative`'s path relative to the
    vault root on the backup server — the upload-direction counterpart to
    sync_vault_file_down, used by backend.py's correct_session_instrument
    to write a correction back to a session_log.json that only exists on
    the backup server (session_vault_mode "remote" already pruned the
    local copy — see vault.sync_and_maybe_prune)."""
    parent_relative = os.path.dirname(vault_relative)
    if parent_relative:
        _ensure_remote_dir(remote, parent_relative)
    dest = _remote_path(remote, vault_relative)
    cmd = ["rsync", "-avz", "--checksum", str(local_path), dest]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def fetch_remote_session_log(remote: str, session_dir_name: str) -> dict | None:
    """One session's session_log.json, fetched fresh from the backup
    server via a scratch temp file that's deleted immediately after —
    never written into the local vault, so there's nothing here to
    double as a second, possibly-stale copy of session data. Used by
    Backend.get_session_detail/correct_session_instrument/reassign_take
    when session_vault_mode "remote" has already pruned the session's
    local directory. Returns None on any failure (host unreachable,
    session doesn't exist there either, malformed JSON)."""
    vault_relative = f"sessions/{session_dir_name}/session_log.json"
    with tempfile.TemporaryDirectory() as tmp:
        local_path = Path(tmp) / "session_log.json"
        if not sync_vault_file_down(remote, vault_relative, local_path):
            return None
        try:
            return json.loads(local_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None


def write_remote_session_log(remote: str, session_dir_name: str, data: dict) -> bool:
    """Write `data` back as session_dir_name's session_log.json on the
    backup server, via the same kind of scratch temp file fetch_remote_
    session_log uses — nothing persists locally either way. Used by
    correct_session_instrument for a session that only exists remotely."""
    vault_relative = f"sessions/{session_dir_name}/session_log.json"
    with tempfile.TemporaryDirectory() as tmp:
        local_path = Path(tmp) / "session_log.json"
        local_path.write_text(json.dumps(data, indent=2))
        return sync_vault_file_up(local_path, vault_relative, remote)


# Marks where one session's session_log.json contents begin in
# fetch_remote_session_logs' single combined SSH round-trip — chosen to be
# extremely unlikely to collide with anything inside real JSON content.
_SESSION_LOG_MARKER = "###TAKELOOM_SESSION###"


def fetch_remote_session_logs(remote: str) -> dict[str, dict]:
    """Every session's session_log.json on the backup server's vault/
    sessions/ folder, in a single SSH round-trip — used by Backend.
    list_sessions() when session_vault_mode is "remote" (vault.
    sync_and_maybe_prune deletes each session's local directory right
    after syncing it, so there's normally nothing local left to scan at
    all). One round-trip per session (as fetch_remote_session_log does)
    would be correct too, just slow once there's a real history of
    sessions to browse — this instead has the remote shell itself walk
    every session directory and print each session_log.json prefixed
    with a marker line, parsed back apart here. Best-effort throughout:
    returns {} on any failure (host unreachable, ssh/vault path missing,
    a malformed entry is just skipped) rather than raising — this is a
    convenience listing the vault itself never depends on."""
    if ":" not in remote:
        return {}
    host, path = remote.split(":", 1)
    remote_sessions_dir = os.path.join(path, "sessions")
    script = (
        f"for d in {shlex.quote(remote_sessions_dir)}/*/; do "
        f'n=$(basename "$d"); '
        f'if [ -f "$d/session_log.json" ]; then '
        f'echo "{_SESSION_LOG_MARKER}$n"; cat "$d/session_log.json"; '
        f"fi; done"
    )
    try:
        result = subprocess.run(["ssh", host, script], capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}

    logs: dict[str, dict] = {}
    # A plain split on the marker, not a line-by-line scan: a
    # session_log.json with no trailing newline (the common case — none
    # of backend.py's own writes add one) runs straight into the next
    # marker's echo on the same line, so anything anchored to "the marker
    # starts a fresh line" silently misses every session after the first.
    for chunk in result.stdout.split(_SESSION_LOG_MARKER)[1:]:  # [0] is empty/preamble before the first marker
        newline = chunk.find("\n")
        if newline == -1:
            continue
        name = chunk[:newline].strip()
        try:
            logs[name] = json.loads(chunk[newline + 1:])
        except json.JSONDecodeError:
            continue
    return logs
