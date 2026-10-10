# takeloom-vault

The remote Studio Vault server. Takeloom uploads each recorded session here once it's
been processed, and uses it for every other remote-vault access too: Sessions-tab listing,
`session_log.json` corrections, deletes, and downloading backing tracks and completed takes
that were pruned locally. It also serves a web UI for browsing sessions and watching
their videos.

Stdlib-only Python (3.9+), no dependencies, so it installs anywhere with plain `pipx`.

## Install (on the storage machine, e.g. tank2)

```bash
pipx install "git+https://github.com/priestc/takeloom.git#subdirectory=vault_server"
takeloom-vault install-service --vault "/path/to/Takeloom Studio Vault"
loginctl enable-linger "$USER"   # keep it running while logged out
```

`install-service` writes and starts a systemd **user** unit (`takeloom-vault.service`), so
the server runs as the user who owns the vault files. Logs:
`journalctl --user -u takeloom-vault -f`.

To upgrade: `pipx install --force "git+https://github.com/priestc/takeloom.git#subdirectory=vault_server"`,
then `systemctl --user restart takeloom-vault`.

It listens on port **51824** (takeloom assumes that port when given a bare hostname). Only
local-network peers are allowed (private, loopback, link-local and Tailscale addresses); there's
no TLS. It's LAN-trust, the same as takeloom's own Remote mode.

## Pairing

1. In takeloom's Studio Setup tab, enter the vault's address (e.g. `tank2`) next to
   "Remote vault server" and click **Pair...** (or run `takeloom vault pair tank2`). Takeloom
   shows a 6-digit code.
2. Open `http://tank2:51824` in a browser. The pending request is listed at the top with the
   same code. Click **Approve**.
3. Takeloom receives its token and stores it in `~/studio_config.json`. Requests expire after
   5 minutes.

Paired devices are listed at `/clients` in the web UI, where they can be revoked. You can also
use `takeloom-vault clients` / `takeloom-vault revoke ID`. Only a SHA-256 of each token is
stored (in `~/.config/takeloom-vault/clients.json`).

## Web UI

- `/`: every session in the vault, newest first, plus any pending pairing requests.
- `/sessions/<name>`: the session's video (`session_video.mp4`), with a jump button for
  each song's start; the takes it filed (linked to their audio/video in `completed_takes/`);
  and its raw files (FLAC with an inline player, MIDI, the log).

No login: anyone on the local network can browse. The API needs a paired token.

## API

See the docstring at the top of `takeloom_vault/server.py`. Uploads are `PUT
/api/files/<vault path>` with an `X-Content-SHA256` header. The file is written beside its
destination and only renamed into place once the hash matches. Takeloom uploads a
session's `session_log.json` last, and a session is only listed once that file exists, so an
interrupted upload never looks complete.
