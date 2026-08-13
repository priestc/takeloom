"""Entry point for Takeloom CLI."""

from __future__ import annotations

import sys
import select
import termios
import time
import tty
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from .audio.engine import AudioEngine

from .config import (
    DEFAULT_CONFIG_PATH,
    StudioConfig,
    InputLabel,
    Instrument,
    INSTRUMENT_LABELS,
    VALID_SAMPLE_RATES,
    VALID_BUFFER_SIZES,
)
from .project import Project, Setlist, TrackEntry
from .audio.devices import resolve_device as _resolve_device
from .audio.formats import SUPPORTED_EXTS, get_duration
from .utils import format_duration


@click.group()
def main() -> None:
    """Takeloom - Music Recording Session Manager."""


def _resolve_project(config: StudioConfig, name: str | None) -> Project:
    """Open a project by name, falling back to the last-used one — a
    project is just <projects_dir>/<name>.json now (see project.py), not
    a folder to cd into, so every CLI command that used to infer "the
    current project" from cwd needs a name instead."""
    name = name or config.last_selected_project
    if not name:
        click.echo("Error: no project name given, and no last-used project in config.", err=True)
        raise SystemExit(1)
    setlist_path = Path(config.projects_dir) / f"{name}.json"
    if not setlist_path.exists():
        click.echo(f"Error: project '{name}' not found ({setlist_path}).", err=True)
        raise SystemExit(1)
    return Project.open(setlist_path, Path(config.session_vault_path))


@main.command()
def setup_studio() -> None:
    """Configure studio name, location, musician, and backup server."""
    click.echo("=== Studio Setup ===\n")

    existing = StudioConfig.load()

    existing.studio_name = click.prompt("Studio name", default=existing.studio_name, show_default=bool(existing.studio_name))
    existing.studio_location = click.prompt("Studio location", default=existing.studio_location, show_default=bool(existing.studio_location))
    existing.studio_musician = click.prompt("Studio musician (default performer)", default=existing.studio_musician, show_default=bool(existing.studio_musician))
    existing.backup_server = click.prompt("Backup server (user@host:/path, or empty to skip)", default=existing.backup_server, show_default=bool(existing.backup_server))
    existing.inspiration_server = click.prompt("Inspiration server URL (or empty to skip)", default=existing.inspiration_server, show_default=bool(existing.inspiration_server))
    existing.inspiration_api_key = click.prompt("Inspiration API key (or empty to skip)", default=existing.inspiration_api_key, show_default=bool(existing.inspiration_api_key))

    errors = existing.validate()
    if errors:
        for e in errors:
            click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    existing.save()
    click.echo(f"\nConfig saved to {DEFAULT_CONFIG_PATH}")


@main.command()
def setup_recording_devices() -> None:
    """Configure audio devices, sample rate, buffer size, and input labels."""
    click.echo("=== Recording Devices Setup ===\n")

    existing = StudioConfig.load()

    # Query available audio devices
    devices = []
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        click.echo("Available audio devices:")
        for i, d in enumerate(devices):
            ins = d["max_input_channels"]
            outs = d["max_output_channels"]
            click.echo(f"  [{i}] {d['name']}  (in={ins}, out={outs})")
        click.echo()
    except Exception:
        click.echo("Could not query audio devices (sounddevice unavailable).\n")

    # Sample rate
    sr_choices = [str(r) for r in VALID_SAMPLE_RATES]
    sample_rate = click.prompt(
        "Sample rate",
        type=click.Choice(sr_choices),
        default=str(existing.sample_rate),
    )

    # Buffer size
    buf_choices = [str(b) for b in VALID_BUFFER_SIZES]
    buffer_size = click.prompt(
        "Buffer size",
        type=click.Choice(buf_choices),
        default=str(existing.buffer_size),
    )

    # Output device
    if devices:
        click.echo("Output devices:")
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0:
                click.echo(f"  [{i}] {d['name']}  ({d['max_output_channels']} channels)")
    out_idx = click.prompt("Output device index", type=int, default=0)
    output_device_name = devices[out_idx]["name"] if devices else ""
    if existing.output_device:
        output_device_name = click.prompt("Output device name", default=existing.output_device)
    else:
        click.echo(f"  Selected: {output_device_name}")
    max_out = devices[out_idx]["max_output_channels"] if devices else 2
    output_channels = click.prompt("Output channels", type=int, default=existing.output_channels if existing.output_channels <= max_out else min(2, max_out))

    # Latency compensation
    default_comp = existing.latency_compensation_ms
    if default_comp == 0.0:
        default_comp = round(int(buffer_size) / int(sample_rate) * 1000, 1)
    latency_compensation_ms = click.prompt(
        "Latency compensation (ms)",
        type=float,
        default=default_comp,
    )

    # --- Audio Interface & Input Setup ---
    input_labels: list[InputLabel] = list(existing.input_labels)
    if devices:
        click.echo("\n--- Audio Interface Setup ---")
        input_devs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
        if input_devs:
            click.echo("Available input devices:")
            existing_dev_names = {il.device for il in input_labels}
            for i, d in input_devs:
                marker = " *" if d["name"] in existing_dev_names else ""
                click.echo(f"  [{i}] {d['name']}  ({d['max_input_channels']} ch){marker}")
            if existing_dev_names:
                click.echo("  (* = already configured)")
            click.echo()

            existing_indices = []
            for i, d in input_devs:
                if d["name"] in existing_dev_names:
                    existing_indices.append(str(i))
            default_sel = ",".join(existing_indices) if existing_indices else ""

            sel = click.prompt(
                "Select interface(s) (comma-separated indices, or empty to skip)",
                default=default_sel, show_default=bool(default_sel),
            ).strip()

            selected_devs = []
            if sel:
                for s in sel.split(","):
                    s = s.strip()
                    if s.isdigit():
                        idx = int(s)
                        if 0 <= idx < len(devices) and devices[idx]["max_input_channels"] > 0:
                            selected_devs.append((idx, devices[idx]))

            new_labels: list[InputLabel] = []
            for dev_idx, dev in selected_devs:
                dev_name = dev["name"]
                max_ch = dev["max_input_channels"]
                click.echo(f"\n  Interface: {dev_name} ({max_ch} channels)")

                existing_for_dev = {il.channel: il.label for il in input_labels if il.device == dev_name}

                if existing_for_dev:
                    default_chs = ",".join(str(ch) for ch in sorted(existing_for_dev.keys()))
                else:
                    default_chs = "1"
                ch_sel = click.prompt(
                    f"  Channels to use (1-{max_ch}, comma-separated)",
                    default=default_chs,
                ).strip()

                channels = []
                for c in ch_sel.split(","):
                    c = c.strip()
                    if c.isdigit():
                        ch = int(c)
                        if 1 <= ch <= max_ch:
                            channels.append(ch)

                for ch in channels:
                    default_label = existing_for_dev.get(ch, f"{dev_name} Ch{ch}")
                    label = click.prompt(f"  Label for channel {ch}", default=default_label)
                    new_labels.append(InputLabel(label=label, device=dev_name, channel=ch))

            if new_labels:
                input_labels = new_labels

    # --- Camera Setup ---
    click.echo("\n--- Camera Setup ---")
    from .video.devices import list_cameras, ffmpeg_available
    camera_device = existing.camera_device
    camera_label = existing.camera_label
    if not ffmpeg_available():
        click.echo("ffmpeg not found; camera recording will be unavailable.")
    else:
        cameras = list_cameras()
        if cameras:
            click.echo("Available cameras:")
            for device_id, name in cameras:
                marker = " *" if device_id == existing.camera_device else ""
                click.echo(f"  [{device_id}] {name}{marker}")
            sel = click.prompt(
                "Select camera device id (leave empty to disable camera recording)",
                default=existing.camera_device, show_default=bool(existing.camera_device),
            ).strip()
            if not sel:
                camera_device = ""
                camera_label = ""
            else:
                camera_device = sel
                camera_label = next((name for dev_id, name in cameras if dev_id == sel), "")
        else:
            click.echo("No cameras detected.")

    existing.sample_rate = int(sample_rate)
    existing.buffer_size = int(buffer_size)
    existing.output_device = output_device_name
    existing.output_channels = output_channels
    existing.latency_compensation_ms = latency_compensation_ms
    existing.input_labels = input_labels
    existing.camera_device = camera_device
    existing.camera_label = camera_label

    errors = existing.validate()
    if errors:
        for e in errors:
            click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    existing.save()
    click.echo(f"\nConfig saved to {DEFAULT_CONFIG_PATH}")
    if input_labels:
        click.echo("Inputs:")
        for il in input_labels:
            click.echo(f"  - {il.label} ({il.device} ch{il.channel})")
    if camera_device:
        click.echo(f"Camera: {camera_label} ({camera_device})")
    else:
        click.echo("Camera: none (video recording disabled)")


@main.command(name="ui")
@click.option(
    "--remote", "remote_ip", default=None,
    help="Connect to a remote takeloom instance at this IP/host on launch (e.g. --remote=192.168.1.190). "
         "Uses a stored token if this host is already a known remote (Remote tab); otherwise this "
         "triggers a pairing request that the other machine's user must approve.",
)
def ui_command(remote_ip: str | None) -> None:
    """Launch the graphical Takeloom interface."""
    from . import update_check
    update_check.check_and_restart(log=click.echo)

    from .ui.app import run
    run(remote_ip=remote_ip)


@main.command(name="detect-test")
def detect_test_command() -> None:
    """Open a standalone window listing every configured instrument and
    recording device, and light up an instrument the moment it's heard
    playing on its own input — a hardware/cabling check independent of
    the main app. Local hardware only, same as the detect-all mechanism
    it's built on (see Backend.start_detect_all's docstring) — there's no
    --remote option."""
    from . import update_check
    update_check.check_and_restart(log=click.echo)

    from .ui.detect_test import run
    run()


@main.command(name="server")
@click.option(
    "--disable-color", is_flag=True, default=False,
    help="Strip ANSI color codes from server log output.",
)
def server_command(disable_color: bool) -> None:
    """Run the remote-control server. This is the only way to host a
    takeloom instance for other clients to connect to — the GUI's Remote tab
    is connect-only. Always listens on the fixed remote.protocol.
    REMOTE_SERVER_PORT (not configurable — see that module for why), and
    only accepts connections from the local network.
    """
    from .backend import LocalBackend, StartRecordingRequest
    from .device_check import check_configured_devices
    from .recording_driver import RecordingDeckDriver
    from .remote.protocol import REMOTE_SERVER_PORT
    from .remote.server import RemoteServer

    def log(msg: str, err: bool = False) -> None:
        # Errors print in red so they stand out in a scrolling headless log;
        # everything else stays the terminal's default color. Detected by the
        # explicit `err` flag or an "error" substring, since most log lines
        # here (and the ones threaded through RemoteServer/RecordingDeckDriver,
        # whose `log` callback only takes a message) don't carry a separate
        # severity of their own.
        is_error = err or "error" in msg.lower()
        if disable_color:
            click.echo(msg, err=err)
        else:
            click.secho(msg, fg="red" if is_error else None, err=err)

    backend = LocalBackend()
    listen_port = REMOTE_SERVER_PORT

    # Deliberately no sleep_guard.track_backend(backend) here: a headless
    # server machine should be free to let its own screensaver/sleep kick
    # in regardless of recording state — only a UI actually being watched
    # (local or a Remote client) has a reason to hold it off. See
    # AppState.recording_active, which does that for the UI side.

    def on_streaming_event(event: str, data: dict) -> None:
        # The only backend event this headless console prints on its own
        # (everything else is either driven by StreamDeck key presses,
        # which RecordingDeckDriver already narrates via `log`, or is
        # display-only state a connected Remote client would show). A
        # streaming session has no on-screen status anywhere in this
        # context otherwise, so without this, a stream starting, YouTube
        # accepting (or rejecting) the title API calls, and the stream
        # ending would all happen invisibly here.
        if event == "streaming_status" and "status" in data:
            log(data["status"])

    backend.on_event(on_streaming_event)

    def request_authorization(ip: str, client_name: str) -> bool:
        log(f"\nPairing request from '{client_name}' ({ip})")
        try:
            return click.confirm("Approve this connection?", default=False)
        except click.exceptions.Abort:
            # No TTY attached to stdin (e.g. this process was started
            # detached/backgrounded) — click.confirm can't prompt at all in
            # that case and raises instead of returning. Deny rather than
            # let this crash the connection-handler thread with no response
            # ever sent back to the waiting client.
            log("Cannot prompt for approval (no interactive terminal attached) — denying.", err=True)
            return False

    server = RemoteServer(backend, listen_port, request_authorization, log=log)
    try:
        server.start()
    except OSError as e:
        log(f"Error: could not start server: {e}", err=True)
        raise SystemExit(1)

    log(f"takeloom server listening on port {listen_port} (host: {backend.hostname()}, ip: {backend.ip_address()})")
    # StreamDeck is checked separately below, via the real connection attempt
    # (driver.connect()), which reports a more specific error than a plain
    # not-found when a device is selected but fails to open.
    for warning in check_configured_devices(backend, include_streamdeck=False):
        log(warning, err=True)

    # Best-effort: open live monitoring for the last-used instrument right
    # away, so the operator can hear themselves in headphones immediately —
    # not only once a take/session/video-check actually starts. See
    # LocalBackend.start_monitoring().
    if backend.start_monitoring():
        log(f"Live-monitoring '{backend.get_config().last_selected_instrument}'.")

    # Optional attached StreamDeck: fully drives a session with no UI client
    # needed at all, via the same RecordingDeckDriver the Tk UI uses. With
    # no track picker of its own, this context always targets the last-used
    # project + instrument (from config) and the next untaken track.
    def _resolve_headless_request() -> StartRecordingRequest | None:
        cfg = backend.get_config()
        project_name, instrument_name = cfg.last_selected_project, cfg.last_selected_instrument
        if not project_name or not instrument_name:
            log("StreamDeck: no last-used project/instrument — start one from a connected client first.")
            return None
        index = backend.next_untaken_track_index(project_name, instrument_name)
        if index is None:
            log(f"StreamDeck: no more tracks in '{project_name}' need a take for '{instrument_name}'.")
            return None
        return StartRecordingRequest(
            project_name=project_name, instrument_name=instrument_name, track_index=index,
        )

    def _open_video_check_result(path: Path, has_video: bool) -> None:
        # Always open locally — the server is often where the real
        # monitoring (headphones/speakers) actually lives, so whoever's
        # sitting at it needs to review the result too, not just whoever's
        # on a connected Remote client. Also send it to any connected Remote
        # client (chunked, since it can be tens of MB) so whichever machine
        # an operator is actually sitting at can review it in its own
        # native player.
        from .video.capture import open_in_default_player
        open_in_default_player(path)
        if server.client_count > 0:
            server.broadcast_file("video_check_result", path, extra={"has_video": has_video})

    driver = RecordingDeckDriver(
        backend, resolve_start_request=_resolve_headless_request,
        on_video_check_result=_open_video_check_result, log=log,
    )
    if driver.connect():
        log("StreamDeck connected.")
    elif driver.streamdeck.last_error:
        log(f"StreamDeck: found a device but could not connect — {driver.streamdeck.last_error}", err=True)

    log("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        log("\nStopping server...")
        server.stop()
        driver.disconnect()
        try:
            if backend.is_session_active():
                log("Ending active session...")
                backend.stop_recording()
        except BackendError as e:
            log(f"Error ending session: {e}", err=True)
        backend.join_session_processing()


def _prompt_instrument_label() -> str:
    """Shared "pick a label number" prompt used everywhere a CLI command
    interactively creates a new Instrument (setup_instruments,
    start_session, measure_latency) — mirrors Studio Setup's Label
    dropdown (see config.INSTRUMENT_LABELS)."""
    click.echo("  Label (instrument type):")
    for i, lbl in enumerate(INSTRUMENT_LABELS):
        click.echo(f"    [{i + 1}] {lbl}")
    choice = click.prompt("  Label number", type=int, default=1)
    if 1 <= choice <= len(INSTRUMENT_LABELS):
        return INSTRUMENT_LABELS[choice - 1]
    click.echo("  Invalid choice, using first label.")
    return INSTRUMENT_LABELS[0]


@main.command()
def setup_instruments() -> None:
    """Configure instruments and their input assignments."""
    click.echo("=== Instrument Setup ===\n")

    existing = StudioConfig.load()

    if existing.input_labels:
        click.echo("Available inputs:")
        for i, il in enumerate(existing.input_labels):
            click.echo(f"  [{i + 1}] {il.label}  ({il.device} ch{il.channel})")
        click.echo()
    else:
        click.echo("No inputs configured. Run 'takeloom setup-recording-devices' first.", err=True)
        raise SystemExit(1)

    if existing.instruments:
        click.echo("Existing instruments:")
        for inst in existing.instruments:
            click.echo(f"  - {inst.full_name} ({inst.input_label})")
        click.echo()

    instruments: list[Instrument] = []
    while True:
        if not click.confirm("Add an instrument?", default=bool(not instruments)):
            break
        choice = click.prompt("  Input number", type=int, default=1)
        if 1 <= choice <= len(existing.input_labels):
            input_label_name = existing.input_labels[choice - 1].label
        else:
            click.echo(f"  Invalid choice, using first input.")
            input_label_name = existing.input_labels[0].label
        full_name = click.prompt("  Full name (manufacturer & model)")
        label = _prompt_instrument_label()
        musician = click.prompt("  Musician name", default="", show_default=False)
        instruments.append(Instrument(
            input_label=input_label_name,
            full_name=full_name, label=label, musician=musician,
        ))
        click.echo(f"  Added '{full_name}'.\n")

    if instruments:
        existing.instruments = instruments
    else:
        click.echo("No instruments added; keeping existing config.")

    existing.save()
    click.echo(f"\nConfig saved to {DEFAULT_CONFIG_PATH}")
    if existing.instruments:
        click.echo("Instruments:")
        for inst in existing.instruments:
            click.echo(f"  - {inst.full_name} ({inst.input_label})")


@main.command()
def new_project() -> None:
    """Create a new recording project."""
    config = StudioConfig.load()
    projects_dir = Path(config.projects_dir)

    name = click.prompt("Project name")
    project = Project.create_new(projects_dir, name, Path(config.session_vault_path))
    if config.backup_server:
        project.setlist.backup_server = config.backup_server
        project.save_setlist()

    click.echo(f"Created project: {project.setlist_path}")
    click.echo(f"Add backing tracks to the shared vault's backing_tracks/ folder, then run:")
    click.echo(f"  takeloom update-setlist {name!r}")


@main.command()
def sync_push() -> None:
    """Push every project's setlist file to the backup server. A project
    is just <projects_dir>/<name>.json — this pushes them all at once.
    Recorded sessions, backing tracks, and completed takes sync
    separately via the Studio Session Vault (see 'takeloom setup-studio')."""
    config = StudioConfig.load()
    remote = config.backup_server
    if not remote:
        click.echo("Error: No backup server configured. Set it via 'takeloom setup-studio'.", err=True)
        raise SystemExit(1)

    from .sync import sync_up
    sync_up(Path(config.projects_dir), remote)


@main.command()
def sync_pull() -> None:
    """Pull every project's setlist file from the backup server. See
    sync-push for why this operates on all projects at once."""
    config = StudioConfig.load()
    remote = config.backup_server
    if not remote:
        click.echo("Error: No backup server configured. Set it via 'takeloom setup-studio'.", err=True)
        raise SystemExit(1)

    from .sync import sync_down
    sync_down(Path(config.projects_dir), remote)


@main.command()
@click.argument("project_name", required=False, default=None)
def update_setlist(project_name: str | None) -> None:
    """Add any of the vault's backing tracks not yet in PROJECT_NAME's
    setlist (falls back to the last-used project). backing_tracks/ is
    shared vault-wide across every project now (see 'takeloom
    setup-studio'), so this only adds — it never removes a track for a
    file that's simply used by some other project instead."""
    config = StudioConfig.load()
    project = _resolve_project(config, project_name)

    from .vault import vault_backing_tracks_dir
    backing_dir = vault_backing_tracks_dir(config)
    if not backing_dir.exists():
        click.echo(f"Error: vault backing_tracks/ directory not found ({backing_dir}).", err=True)
        raise SystemExit(1)

    existing_files = {t.backing_track for t in project.setlist.tracks}
    found_files = {
        f.name for f in backing_dir.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS
    }

    added = 0
    for fname in sorted(found_files - existing_files):
        fpath = backing_dir / fname
        try:
            duration = get_duration(fpath)
        except Exception:
            duration = 0.0
        track = TrackEntry(
            name=fpath.stem,
            backing_track=fname,
            duration_seconds=duration,
        )
        project.setlist.add_track(track)
        click.echo(f"  + {fname} ({format_duration(duration)})")
        added += 1

    project.save_setlist()
    click.echo(f"\nSetlist updated: {added} added, {len(project.setlist.tracks)} total.")


@main.command()
@click.argument("project_name", required=False, default=None)
def listen(project_name: str | None) -> None:
    """Listen to mixed takes for a track (without backing track), from
    PROJECT_NAME (falls back to the last-used project)."""
    config = StudioConfig.load()
    project = _resolve_project(config, project_name)
    if not project.setlist.tracks:
        click.echo("No tracks in setlist.")
        raise SystemExit(1)

    # Display tracks with their available takes
    click.echo("=== Tracks ===\n")
    tracks_with_takes = []
    for i, track in enumerate(project.setlist.tracks):
        instruments = list(track.preferred_takes.keys())
        if instruments:
            click.echo(f"  [{i + 1}] {track.name}  ({', '.join(instruments)})")
            tracks_with_takes.append(i)
        else:
            click.echo(f"  [{i + 1}] {track.name}  (no takes)")
    click.echo()

    if not tracks_with_takes:
        click.echo("No tracks have recorded takes yet.")
        raise SystemExit(1)

    choice = click.prompt("Select track number", type=int)
    idx = choice - 1
    if idx < 0 or idx >= len(project.setlist.tracks):
        click.echo("Invalid track number.", err=True)
        raise SystemExit(1)

    track = project.setlist.tracks[idx]
    if not track.preferred_takes:
        click.echo(f"No takes recorded for '{track.name}'.")
        raise SystemExit(1)

    # Import audio modules
    import sounddevice as sd
    from .audio.mixer import Mixer

    # Load all preferred takes into mixer with latency compensation
    mixer = Mixer(config.sample_rate)
    trim = int(config.latency_compensation_ms / 1000.0 * config.sample_rate)
    click.echo(f"\nPlaying: {track.name}")
    for inst_name, take_info in track.preferred_takes.items():
        take_path = project.completed_takes_dir / take_info.filename
        if take_path.exists():
            mixer.add_source(f"take:{inst_name}", take_path, volume=take_info.volume, trim_frames=trim)
            click.echo(f"  + {inst_name}: {take_info.filename}")
        else:
            click.echo(f"  ! {inst_name}: {take_info.filename} (file missing)")

    if not mixer.sources:
        click.echo("No take files found on disk.")
        raise SystemExit(1)

    click.echo(f"\nDuration: {format_duration(mixer.duration_seconds)}")
    click.echo("Press Ctrl+C to stop.\n")

    mixer.set_playing(True)

    # Play through output device
    out_dev = _resolve_device(sd, config.output_device, "output")
    out_info = sd.query_devices(out_dev, "output")
    out_channels = min(config.output_channels, out_info["max_output_channels"])

    def callback(outdata, frames, time_info, status):
        mix = mixer.read(frames)
        if out_channels == 2:
            outdata[:] = mix
        else:
            outdata[:, 0] = mix[:, 0]
        if mixer.is_finished:
            raise sd.CallbackStop

    try:
        with sd.OutputStream(
            samplerate=config.sample_rate,
            blocksize=config.buffer_size,
            device=out_dev,
            channels=max(1, out_channels),
            dtype="float32",
            callback=callback,
        ):
            while mixer.is_playing and not mixer.is_finished:
                sd.sleep(100)
    except KeyboardInterrupt:
        pass

    click.echo("Done.")


@main.command()
@click.argument("instrument")
@click.argument("project_name", required=False, default=None)
def start_session(instrument: str, project_name: str | None) -> None:
    """Start a recording session for INSTRUMENT (its full name — see
    config.py's Instrument, e.g. "Fender Stratocaster"), in PROJECT_NAME
    (falls back to the last-used project). Run 'takeloom sync-pull' first
    if another machine may have newer project files.

    Runs on LocalBackend — the same recording engine and Stream Deck driver
    (RecordingDeckDriver) as the Tk UI and `takeloom server`, so behavior is
    identical across all three. Layered on top: a continuous whole-session
    audio+video recording spanning every track (begin_session()/
    end_session()), which the UI/server don't use.
    """
    from .backend import BackendError, LocalBackend, StartRecordingRequest
    from .recording_driver import RecordingDeckDriver

    # Load config and validate instrument
    config = StudioConfig.load()
    inst = config.get_instrument(instrument)
    if inst is None:
        if not config.input_labels:
            click.echo("Error: No inputs configured. Run 'takeloom setup-recording-devices' first.", err=True)
            raise SystemExit(1)
        click.echo(f"Instrument '{instrument}' not found in config. Let's set it up.\n")
        click.echo("Available inputs:")
        for i, il in enumerate(config.input_labels):
            click.echo(f"  [{i + 1}] {il.label}  ({il.device} ch{il.channel})")
        choice = click.prompt("  Input number", type=int, default=1)
        if 1 <= choice <= len(config.input_labels):
            input_label_name = config.input_labels[choice - 1].label
        else:
            click.echo(f"  Invalid choice, using first input.")
            input_label_name = config.input_labels[0].label
        label = _prompt_instrument_label()
        musician = click.prompt("  Musician name", default=config.studio_musician, show_default=bool(config.studio_musician))
        inst = Instrument(
            input_label=input_label_name,
            full_name=instrument, label=label, musician=musician,
        )
        config.instruments.append(inst)
        config.save()
        click.echo(f"  Saved '{instrument}' to config.\n")

    project = _resolve_project(config, project_name)

    if not project.setlist.tracks:
        click.echo("Error: Setlist is empty. Run 'takeloom update-setlist' first.", err=True)
        raise SystemExit(1)

    backend = LocalBackend()
    # Deliberately no sleep_guard.track_backend(backend) here — see the
    # matching comment in server_command; a CLI recording session is no
    # different from a headless server in that regard.

    click.echo(f"=== Recording Session: {project.name} / {inst.full_name} ===")
    click.echo(f"Tracks: {len(project.setlist.tracks)}")
    click.echo("Controls: [r] record/unpause/stop  [c] video check  [n]ext track  [b] restart take")
    click.echo("          [l]ower volume  [u]p volume  [[]lower takes  []]raise takes")
    click.echo("          [q]uit\n")

    try:
        backend.begin_session(project.name, inst.full_name)
    except BackendError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    def _resolve_cli_request() -> StartRecordingRequest | None:
        index = backend.next_untaken_track_index(project.name, inst.full_name)
        if index is None:
            click.echo(f"All tracks already have a take for '{inst.full_name}'.")
            return None
        return StartRecordingRequest(
            project_name=project.name, instrument_name=inst.full_name, track_index=index,
        )

    def _open_video_check_result(path: Path, has_video: bool) -> None:
        from .video.capture import open_in_default_player
        open_in_default_player(path)

    driver = RecordingDeckDriver(
        backend, resolve_start_request=_resolve_cli_request,
        on_video_check_result=_open_video_check_result, log=click.echo,
    )
    if driver.connect():
        click.echo("StreamDeck connected.")
    elif driver.streamdeck.last_error:
        click.echo(f"StreamDeck: found a device but could not connect — {driver.streamdeck.last_error}")

    # Load the first untaken track, same as the headless server's "r" does
    # on first press — here it happens automatically on launch, matching
    # this command's previous behavior of loading a track immediately.
    req = _resolve_cli_request()
    if req is not None:
        try:
            backend.start_recording(req)
        except BackendError as e:
            click.echo(f"Error: {e}", err=True)
    click.echo()

    # Terminal keystrokes feed the exact same driver.handle_key() the
    # Stream Deck uses — one dispatcher, two input sources (recording_driver.py).
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            if select.select([sys.stdin], [], [], 0.2)[0]:
                key = sys.stdin.read(1).lower()
                if key == "q":
                    break
                driver.handle_key(key)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    driver.disconnect()
    try:
        backend.stop_recording()  # ends the session; no-op if "r" already did
    except BackendError as e:
        click.echo(f"Error ending session: {e}", err=True)
    click.echo("Processing session takes...")
    backend.join_session_processing()
    # Recorded session, backing tracks, and completed takes are already
    # synced to the backup server by LocalBackend itself as part of
    # ending the session (see vault.py) — nothing further to do here.
    # If this project's own setlist.json needs pushing too (e.g. another
    # machine will pick it up), run 'takeloom sync-push'.


@main.command()
@click.argument("instrument")
def measure_latency(instrument: str) -> None:
    """Measure and calibrate latency compensation by ear for INSTRUMENT
    (its full name — see config.py's Instrument, e.g. "Fender
    Stratocaster")."""
    config = StudioConfig.load()
    inst = config.get_instrument(instrument)
    if inst is None:
        if not config.input_labels:
            click.echo("Error: No inputs configured. Run 'takeloom setup-recording-devices' first.", err=True)
            raise SystemExit(1)
        click.echo(f"Instrument '{instrument}' not found in config. Let's set it up.\n")
        click.echo("Available inputs:")
        for i, il in enumerate(config.input_labels):
            click.echo(f"  [{i + 1}] {il.label}  ({il.device} ch{il.channel})")
        choice = click.prompt("  Input number", type=int, default=1)
        if 1 <= choice <= len(config.input_labels):
            input_label_name = config.input_labels[choice - 1].label
        else:
            click.echo("  Invalid choice, using first input.")
            input_label_name = config.input_labels[0].label
        label = _prompt_instrument_label()
        musician = click.prompt("  Musician name", default=config.studio_musician, show_default=bool(config.studio_musician))
        inst = Instrument(
            input_label=input_label_name,
            full_name=instrument, label=label, musician=musician,
        )
        config.instruments.append(inst)
        config.save()
        click.echo(f"  Saved '{instrument}' to config.\n")

    import sounddevice as sd
    from .audio.engine import AudioEngine

    input_info = config.resolve_input(inst.input_label)
    if input_info is None:
        click.echo(f"Error: Input label '{inst.input_label}' not found in config.", err=True)
        raise SystemExit(1)
    in_dev = _resolve_device(sd, input_info.device, "input")
    if in_dev is None:
        click.echo(f"Error: Input device '{input_info.device}' not found.", err=True)
        raise SystemExit(1)
    out_dev = _resolve_device(sd, config.output_device, "output")

    in_info = sd.query_devices(in_dev, "input")
    out_info = sd.query_devices(out_dev, "output")
    input_channel_index = input_info.channel - 1
    input_channels = max(input_info.channel, 1)
    output_channels = min(config.output_channels, out_info["max_output_channels"])

    if input_channels > in_info["max_input_channels"]:
        click.echo(
            f"Error: Instrument '{inst.full_name}' needs input channel {input_channels} "
            f"but device only has {in_info['max_input_channels']} channels.",
            err=True,
        )
        raise SystemExit(1)

    ref_wav = Path(__file__).parent / "data" / "measure_latency.wav"
    if not ref_wav.exists():
        click.echo(f"Error: Reference audio not found at {ref_wav}", err=True)
        raise SystemExit(1)

    import tempfile
    tmp_recording = Path(tempfile.mktemp(suffix=".flac", prefix="takeloom_latency_"))

    engine = AudioEngine(
        sample_rate=config.sample_rate,
        buffer_size=config.buffer_size,
        input_device=in_dev,
        output_device=out_dev,
        input_channels=input_channels,
        output_channels=max(1, output_channels),
        monitor_channel=input_channel_index,
    )
    engine.start()

    try:
        click.echo("=== Latency Measurement ===\n")
        click.echo(f"  Instrument:  {inst.full_name}")
        click.echo(f"  Input:       {input_info.label} ({input_info.device} ch{input_info.channel})")
        click.echo(f"  Output:      {config.output_device}")
        click.echo()
        click.echo("You'll hear a rhythm of beeps ending with a loud HIT tone.")
        click.echo("Clap or hit your instrument exactly on the HIT.\n")

        if _latency_record_phase(engine, ref_wav, tmp_recording):
            _latency_adjust_phase(engine, ref_wav, tmp_recording, config)
    finally:
        engine.stop()
        if tmp_recording.exists():
            tmp_recording.unlink()


def _latency_record_phase(engine: AudioEngine, ref_wav: Path, tmp_recording: Path) -> bool:
    """Record phase: play reference, record clap. Returns True to continue to adjust."""
    while True:
        engine.mixer.clear()
        engine.mixer.add_source("ref", ref_wav)
        engine.start_recording(tmp_recording)
        engine.mixer.reset()
        engine.mixer.set_playing(True)

        click.echo("  Playing reference... clap/hit on the HIT tone!")

        import sounddevice as sd
        while not engine.mixer.is_finished:
            sd.sleep(100)

        engine.stop_recording()
        engine.mixer.set_playing(False)

        click.echo("  Recording captured.")
        action = click.prompt("  [r]etry, [c]ontinue to adjust, [q]uit", type=click.Choice(["r", "c", "q"]))

        if action == "c":
            return True
        elif action == "q":
            return False
        else:
            # Retry — delete recording and loop
            if tmp_recording.exists():
                tmp_recording.unlink()


def _latency_adjust_phase(
    engine: AudioEngine, ref_wav: Path, tmp_recording: Path, config: StudioConfig
) -> None:
    """Adjustment phase: play ref + recording together, adjust trim with u/d keys."""
    latency_ms = config.latency_compensation_ms
    sample_rate = config.sample_rate

    def _load_and_play() -> None:
        trim = int(latency_ms / 1000.0 * sample_rate)
        engine.mixer.clear()
        engine.mixer.add_source("ref", ref_wav)
        engine.mixer.add_source("recording", tmp_recording, trim_frames=trim)
        engine.mixer.reset()
        engine.mixer.set_playing(True)

    _load_and_play()

    click.echo(f"\n  Current latency: {latency_ms:.0f} ms")
    click.echo("  Controls: [u] +5ms  [d] -5ms  [r] replay  [s] save  [q] quit")
    click.echo("  Listening... adjust until the clap aligns with the HIT tone.\n")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        import sounddevice as sd
        while True:
            if select.select([sys.stdin], [], [], 0.2)[0]:
                key = sys.stdin.read(1).lower()

                if key == "u":
                    latency_ms += 5
                    trim = int(latency_ms / 1000.0 * sample_rate)
                    engine.mixer.set_trim("recording", trim)
                    engine.mixer.reset()
                    engine.mixer.set_playing(True)
                    click.echo(f"  Latency: {latency_ms:.0f} ms")

                elif key == "d":
                    latency_ms = max(0, latency_ms - 5)
                    trim = int(latency_ms / 1000.0 * sample_rate)
                    engine.mixer.set_trim("recording", trim)
                    engine.mixer.reset()
                    engine.mixer.set_playing(True)
                    click.echo(f"  Latency: {latency_ms:.0f} ms")

                elif key == "r":
                    engine.mixer.reset()
                    engine.mixer.set_playing(True)
                    click.echo("  Replaying...")

                elif key == "s":
                    config.latency_compensation_ms = latency_ms
                    config.save()
                    click.echo(f"\n  Saved latency_compensation_ms = {latency_ms:.0f} ms to {DEFAULT_CONFIG_PATH}")
                    return

                elif key == "q":
                    click.echo("\n  Quit without saving.")
                    return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()
