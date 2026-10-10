"""takeloom-vault command line.

    takeloom-vault serve --vault PATH [--port 51824]
    takeloom-vault clients                 list paired devices
    takeloom-vault revoke CLIENT_ID
    takeloom-vault install-service --vault PATH [--port 51824]
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import DEFAULT_PORT

STATE_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "takeloom-vault"
CLIENTS_FILE = STATE_DIR / "clients.json"
DEFAULT_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "takeloom-vault" / "take-videos"
SERVICE_NAME = "takeloom-vault.service"


def _cmd_serve(args) -> None:
    from .auth import Auth
    from .server import serve
    from .server import log
    from .store import Vault
    from .takes import TakeVideoCache
    vault = Vault(Path(args.vault))
    if not vault.root.is_dir():
        sys.exit(f"Vault folder does not exist: {vault.root}")
    take_videos = TakeVideoCache(
        vault, Path(args.cache_dir).expanduser(), args.cache_max_age_days, int(args.cache_max_gb * 1e9), log,
    )
    serve(vault, Auth.load(CLIENTS_FILE), take_videos, args.host, args.port)


def _cmd_clients(_args) -> None:
    from .auth import Auth
    clients = Auth.load(CLIENTS_FILE).clients
    if not clients:
        print("No paired devices.")
    for c in clients:
        print(f"{c.id}  {c.label:<24} last {c.last_ip or '?':<16} seen {c.last_seen_at or '-'}  paired {c.paired_at}")


def _cmd_revoke(args) -> None:
    from .auth import Auth
    if not Auth.load(CLIENTS_FILE).revoke(args.client_id):
        sys.exit(f"No paired device with id {args.client_id!r} (see `takeloom-vault clients`).")
    print(f"Revoked {args.client_id}.")


def _cmd_install_service(args) -> None:
    """A systemd *user* unit, so the server runs as the same user who owns
    the vault files. Needs lingering enabled (`loginctl enable-linger`) to
    keep running while that user isn't logged in."""
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        sys.exit(f"Vault folder does not exist: {vault}")
    exe = shutil.which("takeloom-vault") or sys.argv[0]
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / SERVICE_NAME
    exec_start = " ".join(shlex.quote(a) for a in [
        exe, "serve", "--vault", str(vault), "--port", str(args.port), "--cache-dir", str(args.cache_dir),
        "--cache-max-age-days", str(args.cache_max_age_days), "--cache-max-gb", str(args.cache_max_gb),
    ])
    unit.write_text(
        "[Unit]\n"
        "Description=Takeloom Studio Vault server\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    print(f"Wrote {unit}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME], check=True)
    subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], check=True)
    print(f"Started. Logs: journalctl --user -u {SERVICE_NAME} -f")


def _add_cache_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                   help="where take videos cut on demand are kept (default: %(default)s)")
    p.add_argument("--cache-max-age-days", type=float, default=14,
                   help="evict a cached take video not requested for this long (default: %(default)s)")
    p.add_argument("--cache-max-gb", type=float, default=20,
                   help="evict least-recently-used take videos past this total size (default: %(default)s)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="takeloom-vault", description="Takeloom Studio Vault server")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the vault server in the foreground")
    p.add_argument("--vault", required=True, help="the vault folder (holds sessions/, completed_takes/, ...)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    _add_cache_args(p)
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("clients", help="list paired devices")
    p.set_defaults(func=_cmd_clients)

    p = sub.add_parser("revoke", help="unpair a device")
    p.add_argument("client_id")
    p.set_defaults(func=_cmd_revoke)

    p = sub.add_parser("install-service", help="install + start as a systemd user service")
    p.add_argument("--vault", required=True)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    _add_cache_args(p)
    p.set_defaults(func=_cmd_install_service)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
