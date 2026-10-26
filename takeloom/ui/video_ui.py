"""`takeloom video-ui`: the video operator's window.

A thumbnail of every camera along the top (Backend.session_cameras()) and,
front and center, the program ("out") camera: the one the live stream
shows. Click a thumbnail, or press its number key, to make it the program
camera (Backend.set_program_camera): the stream switches to it, and during a
session the switch goes into the session log, so the streamed video can be
rebuilt afterwards from the cameras' recordings.

A separate window from the main UI, which is left as it is for now; the
plan is for this to take over video entirely and the main UI to become
audio-only. Like the main UI it's usually a remote-control terminal for the
Mac Mini's `takeloom server`: --remote, an "always connect" remote, or a
server running on this same machine; otherwise it runs on this machine's own
cameras (no Takeloom Camera phones: only the server hosts those).
"""

from __future__ import annotations

import io
import socket
import tkinter as tk

from ..backend import BackendError
from .app_state import AppState

THUMB_W, THUMB_H = 224, 126
RENDER_INTERVAL_MS = 50  # at most ~20 redraws a second, whatever the cameras send
PROGRAM_COLOR = "#d32f2f"
IDLE_COLOR = "#3a3a3a"
BG = "#111111"


def _fit(image, width: int, height: int):
    """`image` scaled to fit width x height, aspect kept."""
    from PIL import Image
    if width <= 1 or height <= 1:
        return image
    scale = min(width / image.width, height / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.BILINEAR)


class VideoUIFrame(tk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master, background=BG)
        self.app_state = app_state
        self._backend = None
        self._preview_sub = None
        self._cameras: list[tuple[str, str]] = []
        self._program = ""
        self._streaming = False
        self._tiles: dict[str, dict] = {}
        # camera_device -> newest JPEG not yet drawn (written by the preview
        # thread, drawn by _render_tick on the Tk thread).
        self._pending: dict[str, bytes] = {}
        self._program_imgtk = None

        self.strip = tk.Frame(self, background=BG)
        self.strip.pack(side="top", fill="x", padx=12, pady=(12, 8))
        # Packed before the program view, so the picture can never push it
        # out of the window.
        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, background=BG, foreground="#bbbbbb",
                 anchor="w").pack(side="bottom", fill="x", padx=12, pady=8)
        self.program_view = tk.Label(self, background="black", foreground="#888888",
                                     text="No camera", font=("TkDefaultFont", 16))
        self.program_view.pack(side="top", fill="both", expand=True, padx=12)

        self.app_state.add_listener(self._on_app_state_changed)
        self.bind("<Destroy>", self._on_destroy)
        self.winfo_toplevel().bind("<Key>", self._on_key)
        self._attach_backend()
        self.after(RENDER_INTERVAL_MS, self._render_tick)

    # --- backend ---

    def _run(self, fn, on_done=None) -> None:
        """fn() on a worker thread; on_done(result, error) back on Tk's."""
        import threading

        def worker() -> None:
            try:
                result, error = fn(), None
            except BackendError as e:
                result, error = None, str(e)
            if on_done is not None:
                try:
                    self.after(0, lambda: on_done(result, error))
                except RuntimeError:
                    pass  # window closed meanwhile
        threading.Thread(target=worker, daemon=True).start()

    def _attach_backend(self) -> None:
        if self._backend is not None:
            self._backend.off_event(self._on_backend_event)
        if self._preview_sub is not None:
            self._preview_sub.close()
        self._backend = self.app_state.backend
        self._backend.on_event(self._on_backend_event)
        self._preview_sub = self._backend.open_camera_preview(self._on_preview_frame)
        self._refresh()

    def _on_app_state_changed(self) -> None:
        if self.app_state.backend is not self._backend:
            self._attach_backend()

    def _refresh(self) -> None:
        backend = self._backend
        self._run(lambda: (backend.session_cameras(), backend.get_program_camera()), self._on_refreshed)

    def _on_refreshed(self, result, error) -> None:
        if not self.winfo_exists():
            return
        if error or result is None:
            self.status_var.set(error or "Couldn't load the cameras.")
            return
        cameras, program = [tuple(c) for c in result[0]], result[1]
        if cameras != self._cameras:
            self._cameras = cameras
            self._build_strip()
        self._set_program(program)

    def _on_backend_event(self, event: str, data: dict) -> None:
        # From a backend thread: hop to Tk's.
        try:
            self.after(0, lambda: self._handle_event(event, data))
        except RuntimeError:
            pass

    def _handle_event(self, event: str, data: dict) -> None:
        if not self.winfo_exists():
            return
        if event == "program_camera_changed":
            self._set_program(data.get("camera", ""))
        elif event == "session_cameras_changed":
            self._refresh()
        elif event == "streaming_status":
            self._streaming = bool(data.get("active"))
            self._update_status(data.get("status", ""))
            self._paint_selection()
        elif event == "recording_status" and data.get("status"):
            self._update_status(data["status"])

    # --- the camera strip ---

    def _build_strip(self) -> None:
        for child in self.strip.winfo_children():
            child.destroy()
        self._tiles = {}
        if not self._cameras:
            tk.Label(self.strip, text="No cameras. Add one on the main window's Recording Devices tab, "
                                      "or connect Takeloom Camera.",
                     background=BG, foreground="#bbbbbb").pack(anchor="w")
        for index, (device, label) in enumerate(self._cameras, start=1):
            border = tk.Frame(self.strip, background=IDLE_COLOR, padx=3, pady=3, cursor="hand2")
            border.pack(side="left", padx=(0, 10))
            image = tk.Label(border, background="black", width=THUMB_W, height=THUMB_H,
                             text="Waiting…", foreground="#888888", compound="center")
            image.pack()
            caption = tk.Label(border, background=IDLE_COLOR, foreground="white", anchor="w",
                               text=f"{index}  {label}" if index <= 9 else label)
            caption.pack(fill="x")
            for widget in (border, image, caption):
                widget.bind("<Button-1>", lambda _e, d=device: self._choose(d))
            self._tiles[device] = {"border": border, "image": image, "caption": caption,
                                   "label": label, "imgtk": None}
        self._paint_selection()

    def _paint_selection(self) -> None:
        for device, tile in self._tiles.items():
            selected = device == self._program
            color = PROGRAM_COLOR if selected else IDLE_COLOR
            tile["border"].configure(background=color)
            text = tile["caption"].cget("text").removesuffix("  ● LIVE")
            tile["caption"].configure(background=color,
                                      text=text + ("  ● LIVE" if selected and self._streaming else ""))

    def _on_key(self, event) -> None:
        if event.char and event.char.isdigit() and event.char != "0":
            index = int(event.char) - 1
            if index < len(self._cameras):
                self._choose(self._cameras[index][0])

    def _choose(self, device: str) -> None:
        if device == self._program:
            return
        previous = self._program
        self._set_program(device)  # at once; the server confirms with program_camera_changed
        backend = self._backend

        def done(_result, error) -> None:
            if error and self.winfo_exists():
                self._set_program(previous)
                self.status_var.set(f"Couldn't switch cameras: {error}")
        self._run(lambda: backend.set_program_camera(device), done)

    def _set_program(self, device: str) -> None:
        if device == self._program:
            return
        self._program = device
        self._program_imgtk = None
        self.program_view.configure(image="", text="Waiting for the camera…" if device else "No camera")
        self._paint_selection()
        self._update_status()

    def _update_status(self, message: str = "") -> None:
        label = next((lbl for d, lbl in self._cameras if d == self._program), "")
        where = f"  ·  {self.app_state.remote_name}" if self.app_state.remote_name else ""
        program = f"Program: {label}" if label else "No program camera"
        live = "  ·  streaming" if self._streaming else ""
        self.status_var.set(program + live + where + (f"  ·  {message}" if message else ""))

    # --- frames ---

    def _on_preview_frame(self, device: str, jpeg: bytes) -> None:
        self._pending[device] = jpeg  # from the preview thread; drawn by _render_tick

    def _render_tick(self) -> None:
        if not self.winfo_exists():
            return
        pending, self._pending = self._pending, {}
        for device, jpeg in pending.items():
            self._draw(device, jpeg)
        self.after(RENDER_INTERVAL_MS, self._render_tick)

    def _draw(self, device: str, jpeg: bytes) -> None:
        from PIL import Image, ImageTk
        tile = self._tiles.get(device)
        if tile is None and device != self._program:
            return
        try:
            image = Image.open(io.BytesIO(jpeg))
            image.load()
        except Exception:
            return
        if tile is not None:
            tile["imgtk"] = ImageTk.PhotoImage(_fit(image, THUMB_W, THUMB_H))
            tile["image"].configure(image=tile["imgtk"], text="")
        if device == self._program:
            # A few pixels inside the label, so the picture never asks for
            # more room than it has (and grows the window).
            self._program_imgtk = ImageTk.PhotoImage(
                _fit(image, self.program_view.winfo_width() - 4, self.program_view.winfo_height() - 4)
            )
            self.program_view.configure(image=self._program_imgtk, text="")

    def _on_destroy(self, event) -> None:
        if event.widget is not self:
            return
        self.app_state.remove_listener(self._on_app_state_changed)
        if self._preview_sub is not None:
            self._preview_sub.close()
            self._preview_sub = None
        if self._backend is not None:
            self._backend.off_event(self._on_backend_event)


def _local_server_running(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def run(remote_ip: str | None = None) -> None:
    from pathlib import Path

    from ..remote.protocol import REMOTE_SERVER_PORT
    from .app import _show_connect_screen
    from .desktop_entry import WM_CLASS_NAME
    from .platform_style import normalize as normalize_platform_style

    root = tk.Tk(className=WM_CLASS_NAME)
    normalize_platform_style(root)
    root.title("Takeloom Video")
    root.configure(background=BG)
    icon = tk.PhotoImage(file=str(Path(__file__).resolve().parent.parent / "data" / "icon.png"))
    root.iconphoto(True, icon)
    root._takeloom_icon = icon  # keep a reference or Tk drops the image
    root.geometry("1280x820")

    app_state = AppState()

    def build() -> None:
        VideoUIFrame(root, app_state).pack(fill="both", expand=True)

    app_state.build_ui = build

    # Which takeloom to drive: --remote, else an "always connect" remote,
    # else a server on this machine (the Mac Mini), else this machine itself.
    config = app_state.local_backend.get_config()
    host = remote_ip
    if host is None:
        always = next((r for r in config.known_remotes if r.always_connect), None)
        if always is not None:
            host = always.host
        elif _local_server_running(REMOTE_SERVER_PORT):
            host = "127.0.0.1"
    if host is None:
        build()
    else:
        known = next((r for r in config.known_remotes if r.host == host), None)
        _show_connect_screen(root, app_state, host, REMOTE_SERVER_PORT, known.token if known else "",
                             on_connected=build)
    root.mainloop()
