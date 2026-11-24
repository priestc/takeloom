"""Takeloom graphical interface entry point."""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

from tkinterdnd2 import TkinterDnD

from ..backend import BackendError
from ..device_check import check_configured_devices
from .app_state import AppState
from .platform_style import normalize as normalize_platform_style
from .record import RecordFrame
from .recording_devices import RecordingDevicesFrame
from .studio_setup import StudioSetupFrame
from .this_computer import ThisComputerFrame

# (title, frame class, persistent). Persistent tabs are built once and never
# torn down on tab switches — needed for Record, which holds a live audio
# stream / ffmpeg process / camera handle that a rebuild would kill outright.
# (Completed takes, and their mixer, are on the vault's web UI now; so is
# controlling another machine — there's no remote mode: every UI drives its
# own machine's hardware, and the vault's web UI reaches the recording nodes.)
TABS = [
    ("Record", RecordFrame, True),
    ("Studio Setup", StudioSetupFrame, False),
    ("Recording Devices", RecordingDevicesFrame, False),
    ("This Computer", ThisComputerFrame, False),
]


def _build_tabs(root: tk.Misc, app_state: AppState, select_title: str) -> None:
    """Build the tabbed interface (Record, Studio Setup, etc.) and select
    the given tab."""
    # Configured-but-not-connected devices (mic, camera, Stream Deck) — shown
    # above the notebook so it stays visible no matter which tab is active,
    # not buried in a single tab's own log.
    device_warnings = check_configured_devices(app_state.local_backend)
    if device_warnings:
        warning_label = ttk.Label(
            root, text="\n".join(f"⚠ {w}" for w in device_warnings),
            foreground="#b00020", justify="left", wraplength=1000,
        )
        warning_label.pack(fill="x", padx=16, pady=(12, 0))

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


def _process_latest_session_if_pending(root: tk.Misc, app_state: AppState, on_done: Callable[[], None]) -> None:
    """Startup check, before the normal tabbed UI ever appears: if the
    vault's own most recent session hasn't been through process_session
    yet, do that now, with a placeholder screen up in the meantime.
    Covers both the ordinary case (the app just hasn't gotten to it) and
    recovering one left behind by a crash or power loss mid-recording —
    nothing else ever retries a pending session on its own, so without
    this it would just sit there until someone happened to notice (see
    processing/splicer.py's total_frames for the recovery side of this).

    Calls `on_done()` exactly once, unconditionally, whether a session
    needed processing, processing succeeded, failed, or nothing was
    found at all — this only ever delays reaching the normal UI, never
    blocks it outright, since silently getting stuck here would be far
    worse than a failed recovery attempt the operator can still retry
    later from a still-pending session."""
    backend = app_state.local_backend
    placeholder = ttk.Frame(root)

    def show_screen(latest: dict) -> None:
        placeholder.pack(fill="both", expand=True, padx=16, pady=16)
        ttk.Label(
            placeholder, text="Processing last session...", font=("TkDefaultFont", 15, "bold"),
        ).pack(pady=(120, 4))
        ttk.Label(placeholder, text=latest.get("date", ""), foreground="#666666").pack()

    def finish() -> None:
        if placeholder.winfo_exists():
            placeholder.destroy()
        on_done()

    def worker() -> None:
        try:
            sessions = backend.list_sessions()
        except BackendError as e:
            print(f"takeloom: could not check for a pending session: {e}")
            root.after(0, finish)
            return
        # Only one still on this machine (see takeloom server's same check).
        local = [s for s in sessions if s.get("local")]
        latest = local[0] if local else None
        if latest is None or latest.get("processed", True):
            root.after(0, finish)
            return
        root.after(0, lambda: show_screen(latest))
        try:
            summary = backend.process_pending_session(latest["session_dir"])
            print(f"takeloom: processed last session '{latest['session_dir']}': {summary}")
        except BackendError as e:
            print(f"takeloom: could not process last session '{latest['session_dir']}': {e}")
        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def run() -> None:
    from .desktop_entry import WM_CLASS_NAME, ensure_desktop_entry
    ensure_desktop_entry()
    # a plain tk.Tk() can't accept drag-and-drop (used by New Project);
    # className: what Linux docks match against takeloom.desktop
    root = TkinterDnD.Tk(className=WM_CLASS_NAME)
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

    # Land on Studio Setup until a studio has been configured (studio_name is
    # the identity field CLI setup treats as "configured"); once one exists,
    # open straight to Record, the tab actually used session to session.
    initial_title = "Studio Setup" if not config.studio_name else "Record"
    _build_tabs(root, app_state, select_title=initial_title)
    app_state.start_local_monitoring()
    root.mainloop()
