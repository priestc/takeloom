# Deployment

Always push every commit to `origin` (`main`) right after making it — no need to ask first. This is a standing authorization for `git push` specifically in this repo; it doesn't extend to other destructive/shared-state git operations (force-push, reset --hard, etc.), which still need confirmation as usual.

There are two machines running `takeloom`, installed differently:

- The laptop ("Framework"), reachable via `ssh framework` (non-interactive/key-auth works). Installed via `pipx` directly from this GitHub repo (`takeloom @ git+https://github.com/priestc/takeloom.git`) — not an editable clone, so picking up new code requires a reinstall, not a `git pull`.
- The Mac Mini — the studio hub machine with the Scarlett 4i4 attached (see `studio_hardware_scarlett_4i4` memory), and normally the machine Claude Code itself is already running on (`hostname` reports `Mac.local`). Installed via `pipx install --editable ~/Documents/GitHub/takeloom` — an editable install pointing straight at this working copy (confirm with `cat ~/.local/pipx/venvs/takeloom/lib/python*/site-packages/takeloom*.dist-info/direct_url.json`, should show `"editable": true`). Any commit — or even an uncommitted local edit — takes effect the next time `takeloom` is launched there; no reinstall step needed. (If a dependency is ever added/changed in `pyproject.toml`, that *does* still need `pipx install --editable --force ~/Documents/GitHub/takeloom` to pick up the new package.)

Any change **except code that only affects server mode** (i.e. changes confined to the headless `takeloom server` path — see `takeloom/__main__.py`'s `server_command` and anything only reachable from it) should be automatically deployed once committed:

1. Push to `origin` (`main`). That's the only step to take here — do not `ssh framework` to reinstall.
2. The laptop no longer gets deployed to manually. `takeloom ui` checks GitHub's `main` on launch and self-updates (reinstalls + restarts) if it's behind — see `takeloom/update_check.py`. The user's habit is to close and reopen the app on the laptop before testing, which is what actually picks up the new commit; nothing needs to happen on the laptop from this side beyond the push.
3. Nothing further needed for the Mac Mini — just make sure `takeloom` (or `takeloom server`) is restarted there to pick up the change.

Manual laptop reinstall (reference only — for troubleshooting if auto-update ever fails or misbehaves, not part of the normal flow above):
`ssh framework "pipx install --force git+https://github.com/priestc/takeloom.git"`

# Testing setup

- All actual hardware — audio interfaces (the Scarlett 4i4) and the Stream Deck — is attached to the **Mac Mini**, and only the Mac Mini ever runs `takeloom server` or performs an actual recording. Nothing is ever plugged into the laptop.
- The **laptop ("Framework")** only ever runs `takeloom` in remote mode — connecting to the Mac Mini's `takeloom server` via the Remote tab / `--remote=IP` — to control a session running on the Mac Mini. It never has a device attached and a recording is never started or written locally on it, so the CLI recording paths (`start-session`, `inspiration`) are effectively unused/untested there in practice; treat bugs reported "on the laptop" as remote-mode bugs first.
- The laptop sleeps whenever it's unplugged, which is its normal state (it's rarely plugged in). Keep this in mind for anything that assumes it stays awake/reachable — e.g. it can drop off the network mid-session, and any "don't sleep while recording" behavior only matters there for the remote-viewing UI, not a local recording session.
- On the Mac Mini, the Scarlett 4i4 and Stream Deck are usually powered off between sessions. You're allowed to launch `takeloom ui` there to visually verify pure-UI changes (screenshot it, check it looks right, close it) — no need to ask first. It's expected and fine for audio devices/instrument meters/the Stream Deck connection to show empty or disconnected in that state; that's not a bug, just means the hardware happens to be off right now. Don't read anything into it unless the change you're testing is specifically about device detection/connection.

# Phone mount

`phone_mount/` is the official Takeloom phone mount (3D-printable, imported from the old `phone-wall-mount` repo with its history). Takeloom Camera software may assume every recording phone is held in one of these: landscape, camera bump overhanging the cradle's open end, fixed on the wall.
