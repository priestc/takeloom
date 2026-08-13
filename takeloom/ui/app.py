"""Takeloom graphical interface entry point."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from tkinterdnd2 import TkinterDnD

from ..backend import BackendError
from ..device_check import check_configured_devices
from .app_state import AppState
from .completed_takes import CompletedTakesFrame
from .latency import LatencyFrame
from .platform_style import normalize as normalize_platform_style
from .record import RecordFrame
from .recording_devices import RecordingDevicesFrame
from .remote import RemoteFrame
from .sessions import SessionsFrame
from .streaming import StreamingFrame
from .studio_setup import StudioSetupFrame

# (title, frame class, persistent). Persistent tabs are built once and never
# torn down on tab switches — needed for Record, which holds a live audio
# stream / ffmpeg process / camera handle that a rebuild would kill outright;
# for Latency, which holds the same during a camera latency test; and for
# Remote, which holds the connected RemoteClient.
TABS = [
    ("Record", RecordFrame, True),
    ("Studio Setup", StudioSetupFrame, False),
    ("Recording Devices", RecordingDevicesFrame, False),
    ("Streaming", StreamingFrame, False),
    ("Sessions", SessionsFrame, False),
    ("Completed Takes", CompletedTakesFrame, False),
    ("Latency", LatencyFrame, True),
    ("Remote", RemoteFrame, True),
]

_RETRY_INTERVAL_MS = 10_000  # auto-retry cadence on the "could not connect" screen


def _build_tabs(root: tk.Misc, app_state: AppState, select_title: str) -> None:
    """Build the normal tabbed interface (Record, Studio Setup, etc.) and
    select the given tab. Used both for ordinary local-mode launches and,
    once connected, for an "always connect" remote — the two cases differ
    only in what's on screen before this point, never in the tabs themselves."""
    # Configured-but-not-connected devices (mic, camera, Stream Deck) — shown
    # above the notebook so it stays visible no matter which tab is active,
    # not buried in a single tab's own log. Checked once at startup against
    # this machine's own config/hardware, even when this instance ends up
    # remote-controlling another studio (see check_configured_devices).
    device_warnings = check_configured_devices(app_state.local_backend)
    if device_warnings:
        warning_label = ttk.Label(
            root, text="\n".join(f"⚠ {w}" for w in device_warnings),
            foreground="#b00020", justify="left", wraplength=1000,
        )
        warning_label.pack(fill="x", padx=16, pady=(12, 0))

    status_var = tk.StringVar(value="")
    status_label = ttk.Label(root, textvariable=status_var, foreground="#2a6db0")
    status_label.pack(fill="x", padx=16, pady=(12, 0))

    def on_state_change() -> None:
        status_var.set(f"Remote: {app_state.remote_name}" if app_state.remote_name else "")

    app_state.add_listener(on_state_change)
    on_state_change()

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=16, pady=16)

    containers = []
    for title, frame_cls, persistent in TABS:
        container = ttk.Frame(notebook)
        notebook.add(container, text=title)
        containers.append({"frame": container, "cls": frame_cls, "persistent": persistent, "built": False})

    def rebuild(entry: dict) -> None:
        # Reload from disk each time a tab is shown, so edits saved on one
        # tab (e.g. a new input label) are visible on tabs that depend on it.
        # Persistent tabs are the exception: built once, left alone after.
        if entry["persistent"] and entry["built"]:
            return
        for child in entry["frame"].winfo_children():
            child.destroy()
        entry["cls"](entry["frame"], app_state).pack(fill="both", expand=True)
        entry["built"] = True

    def on_tab_changed(_event: object) -> None:
        index = notebook.index(notebook.select())
        rebuild(containers[index])

    notebook.bind("<<NotebookTabChanged>>", on_tab_changed)

    initial_index = next(i for i, (title, _, _) in enumerate(TABS) if title == select_title)
    notebook.select(initial_index)
    rebuild(containers[initial_index])


def _show_connect_screen(
    root: tk.Misc, app_state: AppState, host: str, port: int, token: str,
    on_connected: Callable[[], None], initial_status: str | None = None,
) -> None:
    """Blocking "connecting" / "waiting for authorization" / "could not
    connect" screen shown in place of the normal tabbed UI until the
    connection actually succeeds — only then does `on_connected()` run.
    A "could not connect" result auto-retries every `_RETRY_INTERVAL_MS`
    in the background as well as offering a manual Retry button, so a
    remote instance recovers on its own once the server comes back.
    Shared by an "always connect" remote at startup (`_run_always_remote`)
    and by recovery from a mid-session disconnect
    (`handle_remote_disconnect`)."""
    from .remote import connect_async, remember_remote_token

    placeholder = ttk.Frame(root)
    placeholder.pack(fill="both", expand=True, padx=16, pady=16)
    retry_after_id: str | None = None

    def clear() -> None:
        nonlocal retry_after_id
        if retry_after_id is not None:
            placeholder.after_cancel(retry_after_id)
            retry_after_id = None
        if not placeholder.winfo_exists():
            return
        for child in placeholder.winfo_children():
            child.destroy()

    def attempt() -> None:
        clear()
        status_var = tk.StringVar(value=initial_status or f"Connecting to {host}...")
        ttk.Label(placeholder, textvariable=status_var, font=("TkDefaultFont", 13)).pack(pady=(60, 0))

        def on_pending() -> None:
            status_var.set(f"Waiting to be authorized by {host}...")

        def on_done(client) -> None:
            remember_remote_token(app_state, host, client)
            placeholder.destroy()
            on_connected()

        def on_error(error: str) -> None:
            nonlocal retry_after_id
            clear()
            ttk.Label(placeholder, text="Could not connect", font=("TkDefaultFont", 14, "bold")).pack(
                pady=(60, 8)
            )
            ttk.Label(placeholder, text=f"{host}: {error}", foreground="#b00020", wraplength=500, justify="center").pack(
                pady=(0, 16)
            )
            ttk.Button(placeholder, text="Retry", command=attempt).pack()
            retry_after_id = placeholder.after(_RETRY_INTERVAL_MS, attempt)

        connect_async(root, app_state, host, port, token, on_done=on_done, on_error=on_error, on_pending=on_pending)

    attempt()


def _run_always_remote(root: tk.Misc, app_state: AppState, host: str, port: int, token: str) -> None:
    """This instance is configured to always connect to a specific remote —
    it must never fall back to showing this machine's own local hardware/
    tabs. Shows the blocking connect/retry screen until the connection
    actually succeeds; only then does the normal tabbed UI (bound to the
    now-remote backend) get built at all."""
    _show_connect_screen(
        root, app_state, host, port, token,
        on_connected=lambda: _build_tabs(root, app_state, select_title="Record"),
    )


def handle_remote_disconnect(root: tk.Misc, app_state: AppState, host: str, port: int, reason: str) -> None:
    """Recover from an unexpected mid-session drop of a remote connection:
    tear down whatever's currently on screen (the tabbed UI) and replace it
    with the same connect/retry screen shown at startup, instead of a
    dialog plus a silent fallback to this machine's own local hardware — a
    remote instance has nothing useful of its own to show while
    disconnected, so it should look and behave just like it does before its
    first successful connect."""
    if not app_state.backend.is_remote():
        return  # already reset by an explicit Disconnect click
    try:
        config = app_state.local_backend.get_config()
        remote = next((r for r in config.known_remotes if r.host == host), None)
        token = remote.token if remote else ""
    except BackendError:
        token = ""

    # Tear down the tabbed UI (and with it every persistent tab's app_state
    # listener, unregistered synchronously by its own <Destroy> handler)
    # before swapping the backend, so the dead RemoteBackend's notification
    # doesn't trigger a doomed reload/rebind in frames about to be destroyed
    # anyway.
    for child in root.winfo_children():
        child.destroy()
    app_state.set_backend(app_state.local_backend)

    _show_connect_screen(
        root, app_state, host, port, token,
        on_connected=lambda: _build_tabs(root, app_state, select_title="Record"),
        initial_status=f"Disconnected ({reason}) — reconnecting to {host}...",
    )


def run(remote_ip: str | None = None) -> None:
    root = TkinterDnD.Tk()  # a plain tk.Tk() can't accept drag-and-drop (used by New Project)
    normalize_platform_style(root)
    root.title("Takeloom")

    icon = tk.PhotoImage(file=str(Path(__file__).resolve().parent.parent / "data" / "icon.png"))
    root.iconphoto(True, icon)
    root._takeloom_icon = icon  # keep a reference or Tk drops the image

    app_state = AppState()
    config = app_state.local_backend.get_config()
    root.geometry(f"{config.window_width}x{config.window_height}")

    def on_close() -> None:
        config = app_state.local_backend.get_config()
        config.window_width = root.winfo_width()
        config.window_height = root.winfo_height()
        app_state.local_backend.save_config(config)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    # An explicit --remote=IP is a one-off, this launch only, and keeps the
    # old best-effort behavior (local tabs come up immediately, connects in
    # the background, a failure is just a dialog). A configured "always
    # connect" remote is a standing instruction that this instance is a
    # remote-control terminal, period — it never shows local mode at all,
    # connected or not.
    always = None if remote_ip else next((r for r in config.known_remotes if r.always_connect), None)

    if always is not None:
        from ..remote.protocol import REMOTE_SERVER_PORT
        _run_always_remote(root, app_state, always.host, REMOTE_SERVER_PORT, always.token)
        root.mainloop()
        return

    # Land on Studio Setup until a studio has been configured (studio_name is
    # the identity field CLI setup treats as "configured"); once one exists,
    # open straight to Record, the tab actually used session to session.
    initial_title = "Studio Setup" if not config.studio_name else "Record"
    _build_tabs(root, app_state, select_title=initial_title)

    if remote_ip:
        from ..remote.protocol import REMOTE_SERVER_PORT
        from .remote import connect_async, remember_remote_token

        match = next((r for r in config.known_remotes if r.host == remote_ip), None)
        host, port, token = (
            (match.host, REMOTE_SERVER_PORT, match.token) if match else (remote_ip, REMOTE_SERVER_PORT, "")
        )

        def on_remote_error(error: str) -> None:
            messagebox.showerror("Could not connect", f"{host}: {error}")

        def on_remote_done(client) -> None:
            remember_remote_token(app_state, host, client)

        connect_async(
            root, app_state, host, port, token,
            on_done=on_remote_done,
            on_error=on_remote_error,
        )

    root.mainloop()
