# Deployment

Always push every commit to `origin` (`main`) right after making it — no need to ask first. This is a standing authorization for `git push` specifically in this repo; it doesn't extend to other destructive/shared-state git operations (force-push, reset --hard, etc.), which still need confirmation as usual.

There are two machines running `takeloom`, installed differently:

- The laptop ("Framework"; a recording node when it records drums, see Testing setup), reachable via `ssh framework-laptop` (the `~/.ssh/config` alias for `framework-laptop.lan`; non-interactive/key-auth works). Installed via `pipx` directly from this GitHub repo (`takeloom @ git+https://github.com/priestc/takeloom.git`) — not an editable clone, so picking up new code requires a reinstall, not a `git pull`.
- The Mac Mini — the studio hub machine with the Scarlett 4i4 attached (see `studio_hardware_scarlett_4i4` memory), and normally the machine Claude Code itself is already running on (`hostname` reports `Mac.local`). Installed via `pipx install --editable ~/Documents/GitHub/takeloom` — an editable install pointing straight at this working copy (confirm with `cat ~/.local/pipx/venvs/takeloom/lib/python*/site-packages/takeloom*.dist-info/direct_url.json`, should show `"editable": true`). Any commit — or even an uncommitted local edit — takes effect the next time `takeloom` is launched there; no reinstall step needed. (If a dependency is ever added/changed in `pyproject.toml`, that *does* still need `pipx install --editable --force ~/Documents/GitHub/takeloom` to pick up the new package.)

Any change **except code that only affects server mode** (i.e. changes confined to the headless `takeloom server` path — see `takeloom/__main__.py`'s `server_command` and anything only reachable from it) should be automatically deployed once committed:

1. Push to `origin` (`main`). That's the only step to take here — do not `ssh framework-laptop` to reinstall.
2. The laptop no longer gets deployed to manually. `takeloom ui` checks GitHub's `main` on launch and self-updates (reinstalls + restarts) if it's behind — see `takeloom/update_check.py`. The user's habit is to close and reopen the app on the laptop before testing, which is what actually picks up the new commit; nothing needs to happen on the laptop from this side beyond the push.
3. Nothing further needed for the Mac Mini — just make sure `takeloom` (or `takeloom server`) is restarted there to pick up the change.

The remote vault server (`takeloom-vault`, this repo's `vault_server/`) runs on **tank2**, reachable via `ssh tank2` — the `~/.ssh/config` alias (user `chris`, key `~/.ssh/id_rsa`). Use the alias: a bare `ssh tank2.lan` skips it and is refused. It's not auto-deployed: after a `vault_server/` change, upgrade it with `ssh tank2 'export PATH="$HOME/.local/bin:$PATH"; pipx install --force "git+https://github.com/priestc/takeloom.git#subdirectory=vault_server" && systemctl --user restart takeloom-vault'` — no need to ask first: upgrading the vault on tank2 is pre-approved, so do it right after pushing any `vault_server/` change.

Manual laptop reinstall (reference only — for troubleshooting if auto-update ever fails or misbehaves, not part of the normal flow above):
`ssh framework-laptop "pipx install --force git+https://github.com/priestc/takeloom.git"`

# Testing setup

- The system is centered on the **vault (tank2)**, which is wired to the router. The Mac Mini and the laptop are **recording nodes**: audio devices plugged in, recording to their own disk and sending to the vault. One node records at a time. The Takeloom Camera phones always connect to the vault (`vault_server/takeloom_vault/camera_hub.py`), not to a node; a node controls them through it (`takeloom/phone_camera/vault_hub.py`), and the node that last started a session (or changed a phone's framing) is in charge of them. The live stream is made on the vault (`live_stream.py`): the recording node sends it only its live mix (`takeloom/phone_camera/vault_stream.py`).
- The **Mac Mini** is the main node: the Scarlett 4i4 and a Stream Deck, `takeloom server` running as a launchd service.
- The **laptop ("Framework", Linux)** is a node occasionally, to record drums: a Scarlett Solo and the Stream Deck plugged in, recording through its own `takeloom ui`, which connects it to the vault's phones (`AppState.start_local_monitoring` → `LocalBackend.connect_vault_cameras`). The rest of the time nothing is plugged into it.
- There is **no remote mode** any more: every takeloom UI (`takeloom ui`, `video-ui`) runs on its own machine's hardware. Controlling a node from elsewhere (the headless Mac Mini especially) is done in the **vault's web UI**: its Cameras page (program camera, each phone's lens, batteries) and Setlists page (setlists; "project" in the code is a setlist file, but the user calls them setlists, so use that word in anything user-facing), which run operations on a node through the vault (`vault_server/takeloom_vault/camera_hub.py`'s `node_rpc` → `takeloom/node_rpc.py`; the allowed ops are `BROWSER_NODE_OPS` in the vault's `server.py`). The Scarlett 4i4 direct-monitor control (`audio/scarlett2_direct_monitor.py`) only ever touches a 4i4, never the Solo.
- The laptop sleeps whenever it's unplugged, which is its normal state (it's rarely plugged in), so `ssh framework-laptop` usually times out. Keep this in mind for anything that assumes it stays awake/reachable — e.g. it can drop off the network mid-session.
- On the Mac Mini, the Scarlett 4i4 and Stream Deck are usually powered off between sessions. You're allowed to launch `takeloom ui` there to visually verify pure-UI changes (screenshot it, check it looks right, close it) — no need to ask first. It's expected and fine for audio devices/instrument meters/the Stream Deck connection to show empty or disconnected in that state; that's not a bug, just means the hardware happens to be off right now. Don't read anything into it unless the change you're testing is specifically about device detection/connection.

# Phone mount

`phone_mount/` is the official Takeloom phone mount (3D-printable, imported from the old `phone-wall-mount` repo with its history). Takeloom Camera software may assume every recording phone is held in one of these: landscape, camera bump overhanging the cradle's open end, fixed on the wall.
