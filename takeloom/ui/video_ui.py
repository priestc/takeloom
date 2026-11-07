"""`takeloom video-ui`: the video operator's window.

A thumbnail of every camera along the top (Backend.session_cameras()) and,
front and center, the program ("out") camera: the one the live stream
shows. The thumbnails run on the small, smooth preview frames; the program
view asks for its camera at full resolution, a few frames a second (see
backend.py's PreviewSubscription), so it's sharp, and over Remote only that
one camera costs full-resolution bandwidth.

A Takeloom Camera phone in the program view can be zoomed, optically only
(Backend.set_camera_framing; phone_camera/framing.py): its lens buttons
below it, or scroll, the −/+ buttons or keys to step from one lens to the
next, and 0 for 1x. No digital zoom and no panning: framing beyond the lens
is done by aiming the phone in its mount. The server remembers each
camera's lens for the next time it connects. Click a thumbnail, or press its number key, to make it the program
camera (Backend.set_program_camera): the stream switches to it, and during a
session the switch goes into the session log, so the streamed video can be
rebuilt afterwards from the cameras' recordings.

Under each Takeloom Camera phone's thumbnail: its battery, and how long
it'll last at the drain rate the server has learned for that phone
(Backend.camera_batteries; phone_camera/battery.py).

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
FRAMING_SEND_INTERVAL_S = 0.1  # while dragging/scrolling: at most 10 framing updates a second
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
        # The program camera's newest full-resolution JPEG not yet drawn.
        self._pending_full: tuple[str, bytes] | None = None
        # Whether the program view has had a full-resolution frame of the
        # program camera: until then (just switched, or a server too old to
        # send them) it shows the small frames instead.
        self._program_full = False
        self._program_imgtk = None
        # The program camera's framing (Backend.get_camera_framing's dict),
        # as shown and as being changed here; None if it can't be framed.
        self._framing: dict | None = None
        self._batteries: dict[str, dict] = {}  # camera device -> battery.summary()
        self._framing_pending: dict | None = None  # newest change not yet sent
        self._framing_sender_busy = False

        self.strip = tk.Frame(self, background=BG)
        self.strip.pack(side="top", fill="x", padx=12, pady=(12, 8))
        self._build_strip()  # "No cameras" until the first refresh says otherwise
        # Packed before the program view, so the picture can never push it
        # out of the window.
        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, background=BG, foreground="#bbbbbb",
                 anchor="w").pack(side="bottom", fill="x", padx=12, pady=8)
        # Zoom/pan controls for the program camera, shown when it has any.
        self.framing_bar = tk.Frame(self, background=BG)
        self.framing_bar.pack(side="bottom", fill="x", padx=12, pady=(8, 0))
        self.program_view = tk.Label(self, background="black", foreground="#888888",
                                     text="No camera", font=("TkDefaultFont", 16))
        self.program_view.pack(side="top", fill="both", expand=True, padx=12)
        self.program_view.bind("<MouseWheel>", self._on_wheel)
        self.program_view.bind("<Button-4>", lambda _e: self._step_lens(1))  # X11 scroll
        self.program_view.bind("<Button-5>", lambda _e: self._step_lens(-1))

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
        self._preview_sub = self._backend.open_camera_preview(self._on_preview_frame, self._on_full_frame)
        self._request_full_resolution()
        self._refresh()

    def _request_full_resolution(self) -> None:
        sub, cameras = self._preview_sub, [self._program] if self._program else []
        if sub is not None:
            self._run(lambda: sub.set_full_resolution(cameras))

    def _on_app_state_changed(self) -> None:
        if self.app_state.backend is not self._backend:
            self._attach_backend()

    def _refresh(self) -> None:
        backend = self._backend
        def load():
            try:
                batteries = backend.camera_batteries()
            except Exception:  # noqa: BLE001 — a server from before battery tracking
                batteries = {}
            return backend.session_cameras(), backend.get_program_camera(), batteries
        self._run(load, self._on_refreshed)

    def _on_refreshed(self, result, error) -> None:
        if not self.winfo_exists():
            return
        if error or result is None:
            self.status_var.set(error or "Couldn't load the cameras.")
            return
        cameras, program, batteries = [tuple(c) for c in result[0]], result[1], result[2]
        self._batteries = dict(batteries or {})
        if cameras != self._cameras:
            self._cameras = cameras
            self._build_strip()
        self._set_program(program)
        self._paint_batteries()

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
        elif event == "camera_framing_changed":
            if data.get("camera") == self._program and "zoom" in data:
                if self._framing_pending is None:
                    self._show_framing(data)  # someone else's change (or the server's clamp)
            elif not data.get("camera"):
                self._refresh_framing()  # a phone's zoom abilities changed
        elif event == "camera_battery_changed" and data.get("camera"):
            self._batteries[data["camera"]] = data
            self._paint_batteries()
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
            tk.Label(self.strip, text="No cameras. Connect one, or open Takeloom Camera on a phone; "
                                      "name or hide them in Settings › Cameras.",
                     background=BG, foreground="#bbbbbb").pack(anchor="w")
        for index, (device, label) in enumerate(self._cameras, start=1):
            border = tk.Frame(self.strip, background=IDLE_COLOR, padx=3, pady=3, cursor="hand2")
            border.pack(side="left", padx=(0, 10))
            # A blank image from the start: a Label's width/height count
            # characters until it holds an image, then pixels.
            blank = tk.PhotoImage(width=THUMB_W, height=THUMB_H)
            image = tk.Label(border, background="black", image=blank, text="Waiting…",
                             foreground="#888888", compound="center")
            image.pack()
            caption = tk.Label(border, background=IDLE_COLOR, foreground="white", anchor="w",
                               text=f"{index}  {label}" if index <= 9 else label)
            caption.pack(fill="x")
            battery = tk.Label(border, background=IDLE_COLOR, foreground="#dddddd", anchor="w", text="")
            for widget in (border, image, caption, battery):
                widget.bind("<Button-1>", lambda _e, d=device: self._choose(d))
            self._tiles[device] = {"border": border, "image": image, "caption": caption, "battery": battery,
                                   "label": label, "imgtk": blank}
        self._paint_selection()
        self._paint_batteries()

    def _paint_batteries(self) -> None:
        """The battery line under each phone's thumbnail; none for a camera
        with no battery (a webcam) or before its first report. Red once
        under 20%, or 30 minutes left, while it isn't charging."""
        from ..phone_camera.battery import describe
        for device, tile in self._tiles.items():
            info = self._batteries.get(device) or {}
            text = describe(info)
            if not text:
                tile["battery"].pack_forget()
                continue
            low = not info.get("charging") and (
                (info.get("level") or 1) < 0.2 or (info.get("minutes_left") is not None and info["minutes_left"] < 30))
            tile["battery"].configure(text="🔋 " + text, foreground="#ff6b6b" if low else "#dddddd")
            tile["battery"].pack(fill="x")

    def _paint_selection(self) -> None:
        for device, tile in self._tiles.items():
            selected = device == self._program
            color = PROGRAM_COLOR if selected else IDLE_COLOR
            tile["border"].configure(background=color)
            tile["battery"].configure(background=color)
            text = tile["caption"].cget("text").removesuffix("  ● LIVE")
            tile["caption"].configure(background=color,
                                      text=text + ("  ● LIVE" if selected and self._streaming else ""))

    def _on_key(self, event) -> None:
        # Only while this view is showing, and not while typing in a field
        # (the Settings tab's camera names, say).
        if not self.winfo_viewable() or isinstance(event.widget, (tk.Entry, tk.Text, tk.Spinbox)):
            return
        if event.char and event.char.isdigit() and event.char != "0":
            index = int(event.char) - 1
            if index < len(self._cameras):
                self._choose(self._cameras[index][0])
        elif event.char in ("+", "="):
            self._step_lens(1)
        elif event.char in ("-", "_"):
            self._step_lens(-1)
        elif event.char == "0":
            self._change_framing(zoom=1.0)

    # --- framing (zoom/pan) ---

    def _refresh_framing(self) -> None:
        device, backend = self._program, self._backend
        if not device:
            self._show_framing(None)
            return

        def done(result, error) -> None:
            if self.winfo_exists() and device == self._program:
                self._show_framing(result if result and result.get("supported") and not error else None)
        self._run(lambda: backend.get_camera_framing(device), done)

    def _show_framing(self, framing: dict | None) -> None:
        """Show `framing` in the bar under the program view. Only rebuilt
        when what the camera can do changes; a zoom/pan just updates the
        zoom readout and lens highlight in place, so a held pan arrow
        (a repeating button) isn't destroyed under the mouse."""
        self._framing = framing
        layout = None if framing is None else tuple(framing.get("lenses") or [])
        if layout != getattr(self, "_framing_layout", None):
            self._framing_layout = layout
            self._build_framing_bar(framing)
        if framing is None:
            return
        self._zoom_label.configure(text=f"{framing['zoom']:.1f}×")
        for lens, b in self._lens_buttons:
            active = abs(framing["zoom"] - lens) < 0.01
            # Only the red ring marks it: macOS draws Tk buttons white, so
            # white text on the active one vanished.
            b.configure(highlightbackground=PROGRAM_COLOR if active else BG)

    def _build_framing_bar(self, framing: dict | None) -> None:
        for child in self.framing_bar.winfo_children():
            child.destroy()
        self._lens_buttons: list[tuple[float, tk.Button]] = []
        if framing is None:
            return

        def button(text, command) -> tk.Button:
            b = tk.Button(self.framing_bar, text=text, command=command, takefocus=False,
                          highlightbackground=BG, foreground="black")
            b.pack(side="left", padx=(0, 4))
            return b

        for lens in framing.get("lenses") or []:
            self._lens_buttons.append((lens, button(f"{lens:g}×", lambda z=lens: self._change_framing(zoom=z))))
        button("−", lambda: self._step_lens(-1))
        self._zoom_label = tk.Label(self.framing_bar, width=6, background=BG, foreground="white")
        self._zoom_label.pack(side="left")
        button("+", lambda: self._step_lens(1))
        hint = "Optical zoom only: scroll or −/+ to change lens; remembered for this camera"
        tk.Label(self.framing_bar, text=hint, background=BG, foreground="#888888").pack(side="left", padx=12)

    def _step_lens(self, direction: int) -> None:
        """To the next lens up (1) or down (-1) from the current zoom."""
        if self._framing is None:
            return
        from ..phone_camera.framing import lenses, nearest_lens
        steps = lenses(self._framing)
        i = steps.index(nearest_lens(self._framing["zoom"], steps))
        self._change_framing(zoom=steps[min(max(i + direction, 0), len(steps) - 1)])

    def _on_wheel(self, event) -> None:
        if event.delta:
            self._step_lens(1 if event.delta > 0 else -1)

    def _change_framing(self, **changes) -> None:
        """Apply locally at once (clamped like the server will), and queue
        for the server: the newest change goes out, at most every
        FRAMING_SEND_INTERVAL_S."""
        if self._framing is None:
            return
        from ..phone_camera.framing import Framing
        f = self._framing
        caps = {"min": f["min"], "max": f["max"], "lenses": f.get("lenses") or [1.0]}
        framing = Framing(changes.get("zoom", f["zoom"]), changes.get("x", f["x"]), changes.get("y", f["y"]))
        framing = framing.clamped(caps)
        self._show_framing({**f, **framing.to_dict()})
        self._framing_pending = {"camera": self._program, **framing.to_dict()}
        if not self._framing_sender_busy:
            self._framing_sender_busy = True
            self._send_framing()

    def _send_framing(self) -> None:
        pending, self._framing_pending = self._framing_pending, None
        if pending is None:
            self._framing_sender_busy = False
            return
        backend = self._backend

        def done(_result, error) -> None:
            if not self.winfo_exists():
                return
            if error:
                self.status_var.set(f"Couldn't zoom/pan: {error}")
            self.after(int(FRAMING_SEND_INTERVAL_S * 1000), self._send_framing)
        self._run(lambda: backend.set_camera_framing(pending["camera"], pending["zoom"], pending["x"], pending["y"]),
                  done)

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
        self._program_full = False
        self._pending_full = None
        self._request_full_resolution()
        self._show_framing(None)
        self._refresh_framing()
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

    def _on_full_frame(self, device: str, jpeg: bytes) -> None:
        self._pending_full = (device, jpeg)  # likewise

    def _render_tick(self) -> None:
        if not self.winfo_exists():
            return
        pending, self._pending = self._pending, {}
        for device, jpeg in pending.items():
            self._draw(device, jpeg)
        full, self._pending_full = self._pending_full, None
        if full is not None and full[0] == self._program:
            image = self._decode(full[1])
            if image is not None:
                self._program_full = True
                self._draw_program(image)
        self.after(RENDER_INTERVAL_MS, self._render_tick)

    @staticmethod
    def _decode(jpeg: bytes):
        from PIL import Image
        try:
            image = Image.open(io.BytesIO(jpeg))
            image.load()
        except Exception:
            return None
        return image

    def _draw_program(self, image) -> None:
        from PIL import ImageTk
        # A few pixels inside the label, so the picture never asks for
        # more room than it has (and grows the window).
        self._program_imgtk = ImageTk.PhotoImage(
            _fit(image, self.program_view.winfo_width() - 4, self.program_view.winfo_height() - 4)
        )
        self.program_view.configure(image=self._program_imgtk, text="")

    def _draw(self, device: str, jpeg: bytes) -> None:
        from PIL import Image, ImageTk
        tile = self._tiles.get(device)
        small_program = device == self._program and not self._program_full
        if tile is None and not small_program:
            return
        image = self._decode(jpeg)
        if image is None:
            return
        if tile is not None:
            # Letterboxed onto a fixed-size thumbnail, so tiles never change size.
            thumb = Image.new("RGB", (THUMB_W, THUMB_H))
            fitted = _fit(image, THUMB_W, THUMB_H)
            thumb.paste(fitted, ((THUMB_W - fitted.width) // 2, (THUMB_H - fitted.height) // 2))
            tile["imgtk"] = ImageTk.PhotoImage(thumb)
            tile["image"].configure(image=tile["imgtk"], text="")
        if small_program:
            self._draw_program(image)

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


def _build_tabs(root: tk.Misc, app_state: AppState) -> None:
    """Cameras (the switcher: VideoUIFrame) and Settings: every video and
    camera setting, moved here from the main window, which is for audio.
    Settings' sections are built the first time they're shown; Latency,
    like on the main window before, is never rebuilt, since it holds a live
    audio stream and camera capture during a test."""
    from tkinter import ttk

    from .camera_settings import CameraSettingsFrame
    from .latency import LatencyFrame
    from .streaming import StreamingFrame

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    cameras_tab = tk.Frame(notebook, background=BG)
    notebook.add(cameras_tab, text="Cameras")
    VideoUIFrame(cameras_tab, app_state).pack(fill="both", expand=True)

    settings = ttk.Notebook(notebook)
    notebook.add(settings, text="Settings")
    sections = []
    for title, cls in (("Cameras", CameraSettingsFrame), ("Streaming", StreamingFrame), ("Latency", LatencyFrame)):
        holder = ttk.Frame(settings, padding=8)
        settings.add(holder, text=title)
        sections.append({"holder": holder, "cls": cls, "built": False})

    def show_section(_event=None) -> None:
        entry = sections[settings.index(settings.select())]
        if not entry["built"]:
            entry["cls"](entry["holder"], app_state).pack(fill="both", expand=True)
            entry["built"] = True

    settings.bind("<<NotebookTabChanged>>", show_section)


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
        _build_tabs(root, app_state)

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
