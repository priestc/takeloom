"""Add to Setlist dialog: add one backing track to a project already
selected on the Record tab, from a local file, a YouTube URL, or the
inspiration server (by exact artist/title search), or add a standing
"inspiration filter" slot that draws a random inspiration-server track
fresh each session — one tab per source, each embedded directly in this
dialog rather than behind its own popup."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from tkinterdnd2 import DND_FILES, DND_TEXT

from ..backend import Backend, BackendError
from ..inspiration import derive_filter_label
from ..youtube import is_youtube_url
from .filter_fields import FilterCriteriaFields


class _AutocompleteEntry(ttk.Frame):
    """An Entry that debounces keystrokes, fetches suggestions on a
    background thread via `fetch(text) -> list[(display_text, payload)]`
    (a Backend call — see docs/inspiration-server-autocomplete-api.md —
    so this works the same over Remote as it does locally), and shows
    them in a small popup below itself. `payload` is opaque to this
    widget — e.g. a full track dict for the Inspiration Title field, so
    picking a suggestion can hand back the exact record the user chose
    instead of just its display string (see `selected_payload`). Pass
    `None` as the payload wherever there's nothing more specific than the
    text itself (e.g. the Artist field).

    Deliberately not a ttk.Combobox: posting a Combobox's built-in
    dropdown (via event_generate("<Down>") or the Tcl Post proc) moves
    keyboard focus into its internal listbox, so a stray letter keystroke
    typed right after suggestions appear lands in the listbox's own
    type-ahead handling instead of the entry — text stops updating
    mid-word. The popup here is a plain, separate Toplevel that never
    takes focus on its own, only when explicitly navigated into (Down
    arrow) or clicked."""

    _DEBOUNCE_MS = 200
    _MAX_VISIBLE = 8

    def __init__(self, master: tk.Misc, fetch: Callable[[str], list[tuple[str, object]]], width: int = 30) -> None:
        super().__init__(master)
        self._fetch = fetch
        self._pending_after_id: str | None = None
        self._popup: tk.Toplevel | None = None
        self._listbox: tk.Listbox | None = None
        self._matches: list[tuple[str, object]] = []
        self.selected_payload: object | None = None

        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(fill="x")
        self.entry.bind("<KeyRelease>", self._on_key_release)
        self.entry.bind("<Down>", self._on_down_from_entry)
        self.entry.bind("<Escape>", lambda _e: self._close_popup())
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Destroy>", self._on_destroy)

    def _on_key_release(self, event: object) -> None:
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab"):  # type: ignore[attr-defined]
            return
        self.selected_payload = None  # any manual edit invalidates a prior exact selection
        if self._pending_after_id is not None:
            self.after_cancel(self._pending_after_id)
        self._pending_after_id = self.after(self._DEBOUNCE_MS, self._kick_off_fetch)

    def _kick_off_fetch(self) -> None:
        self._pending_after_id = None
        text = self.var.get().strip()
        if not text:
            self._close_popup()
            return

        def worker() -> None:
            matches = self._fetch(text)
            self.after(0, lambda: self._apply_results(text, matches))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_results(self, text: str, matches: list[tuple[str, object]]) -> None:
        if not self.winfo_exists() or self.var.get().strip() != text:
            return  # stale response for text the user has since changed — discard it
        if matches:
            self._show_popup(matches)
        else:
            self._close_popup()

    # --- popup ---

    def _show_popup(self, matches: list[tuple[str, object]]) -> None:
        self._matches = matches
        visible = min(len(matches), self._MAX_VISIBLE)
        if self._popup is None:
            self._popup = tk.Toplevel(self)
            self._popup.wm_overrideredirect(True)
            # transient (rather than "-topmost") ties this window to the
            # dialog in the window manager's own bookkeeping, so closing it
            # hands focus back cleanly — an override-redirect "-topmost"
            # window destroyed while it still holds key-window status can
            # otherwise leave the whole parent dialog unresponsive on macOS.
            self._popup.transient(self.winfo_toplevel())
            self._listbox = tk.Listbox(
                self._popup, background="#1a1a1a", foreground="white",
                selectbackground="#2a6db0", highlightthickness=1,
                highlightbackground="#444444", activestyle="none", exportselection=False,
            )
            self._listbox.pack(fill="both", expand=True)
            self._listbox.bind("<ButtonRelease-1>", self._on_listbox_commit)
            self._listbox.bind("<Return>", self._on_listbox_commit)
            self._listbox.bind("<Escape>", lambda _e: self._cancel_popup_navigation())

        self._listbox.configure(height=visible)
        self._listbox.delete(0, tk.END)
        for display_text, _payload in matches:
            self._listbox.insert(tk.END, display_text)

        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        width = self.entry.winfo_width()
        self._popup.update_idletasks()
        height = self._listbox.winfo_reqheight()
        self._popup.wm_geometry(f"{width}x{height}+{x}+{y}")
        self._popup.deiconify()

    def _on_down_from_entry(self, _event: object) -> str | None:
        if self._popup is None or self._listbox is None:
            return None
        self._listbox.focus_set()
        self._listbox.selection_clear(0, tk.END)
        self._listbox.selection_set(0)
        self._listbox.activate(0)
        return "break"  # swallow the keystroke — don't let it also move the entry's cursor

    def _on_listbox_commit(self, _event: object) -> None:
        if self._listbox is None:
            return
        sel = self._listbox.curselection()
        if sel:
            display_text, payload = self._matches[sel[0]]
            self.var.set(display_text)
            self.selected_payload = payload
        self._dismiss_popup_and_refocus()

    def _cancel_popup_navigation(self) -> None:
        self._dismiss_popup_and_refocus()

    def _dismiss_popup_and_refocus(self) -> None:
        # Claim focus back for the entry (and its owning dialog window)
        # BEFORE tearing down the popup, not after — destroying an
        # override-redirect Toplevel while it still holds the OS-level
        # key-window status, with nothing yet claiming it, is what leaves
        # the whole parent dialog dead to clicks/keyboard on macOS. The
        # actual destroy is deferred a tick so this click's own event on
        # the (about to be destroyed) listbox finishes running first.
        self.winfo_toplevel().focus_force()
        self.entry.focus_set()
        self.entry.icursor(tk.END)
        self.after_idle(self._close_popup)

    def _on_focus_out(self, _event: object) -> None:
        # Moving focus into the popup's own listbox (via the Down-arrow
        # handler above) also fires the entry's FocusOut — give that a
        # moment to land before deciding the popup should actually close,
        # otherwise it vanishes the instant Down is pressed.
        self.after(150, self._close_popup_if_focus_elsewhere)

    def _close_popup_if_focus_elsewhere(self) -> None:
        if not self.winfo_exists():
            return
        focused = self.focus_get()
        if focused not in (self.entry, self._listbox):
            self._close_popup()

    def _close_popup(self) -> None:
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None
            self._listbox = None

    def _on_destroy(self, _event: object) -> None:
        if self._pending_after_id is not None:
            self.after_cancel(self._pending_after_id)
            self._pending_after_id = None
        self._close_popup()

    def get(self) -> str:
        return self.var.get()

    def set(self, text: str) -> None:
        self.var.set(text)

    def focus_set(self) -> None:
        self.entry.focus_set()

    def bind_return(self, callback: Callable[[object], None]) -> None:
        self.entry.bind("<Return>", callback)


class AddToSetlistDialog(tk.Toplevel):
    """Add a single backing track to `project_name`'s setlist. Four
    tabs — File, YouTube URL, Inspiration, Inspiration Filter — share one
    Add button that acts on whichever tab is currently selected; switching
    tabs doesn't lose anything typed into the others. `on_track_added`
    fires once, on success, right before the dialog closes itself.

    Inspiration Filter is different from the other three: it doesn't add
    one fixed song, but a standing "slot" that draws a random track
    matching its filter fresh every session — see TrackEntry's docstring
    in project.py and backend.py's _resolve_filter_slot_for_session."""

    _FILE_PLACEHOLDER = "Drag an audio/video file here, or click Browse..."

    def __init__(
        self, master: tk.Misc, backend: Backend, project_name: str, on_track_added: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self.title("Add to Setlist")
        self.resizable(False, False)
        self.transient(master)

        self._backend = backend
        self._project_name = project_name
        self._on_track_added = on_track_added
        self._working = False
        self._file_path = ""
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=f"Adding to: {project_name}", font=("TkDefaultFont", 11, "bold")).pack(
            anchor="w", pady=(0, 8)
        )

        self.notebook = ttk.Notebook(frame)
        self.notebook.pack(fill="both", expand=True)
        self._build_file_tab()
        self._build_youtube_tab()
        self._build_inspiration_tab()
        self._build_inspiration_filter_tab()

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100, length=400)
        self.progress.pack(fill="x", pady=(10, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, foreground="#2a7d2a", wraplength=400).pack(
            anchor="w", pady=(4, 0)
        )

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(12, 0))
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self.destroy)
        self.cancel_button.pack(side="right")
        self.add_button = ttk.Button(footer, text="Add", command=self._on_add)
        self.add_button.pack(side="right", padx=(0, 8))

    # --- File tab ---

    def _build_file_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="File")

        self.file_display_var = tk.StringVar(value=self._FILE_PLACEHOLDER)
        self.file_drop = tk.Label(
            tab, textvariable=self.file_display_var, background="#1a1a1a", foreground="#888888",
            wraplength=380, justify="center", padx=12, pady=28,
            highlightthickness=1, highlightbackground="#444444",
        )
        self.file_drop.pack(fill="x", pady=(0, 8))
        self.file_drop.drop_target_register(DND_FILES, DND_TEXT)
        self.file_drop.dnd_bind("<<Drop>>", self._on_file_drop)

        ttk.Button(tab, text="Browse...", command=self._on_browse_file).pack(anchor="w")

    def _set_file_path(self, path: str) -> None:
        self._file_path = path
        self.file_display_var.set(path)
        self.file_drop.configure(foreground="white")

    def _on_file_drop(self, event: object) -> None:
        items = self._split_dnd_data(event.data)  # type: ignore[attr-defined]
        if not items:
            return
        first = items[0]
        if is_youtube_url(first):
            # Dropped a URL onto the File tab by mistake — route it to
            # where it actually belongs instead of erroring.
            self.youtube_url_var.set(first)
            self.notebook.select(self._youtube_tab)
            return
        self._set_file_path(first)

    @staticmethod
    def _split_dnd_data(data: str) -> list[str]:
        """tkinterdnd2 wraps each dropped item in {...} when it contains
        whitespace, and separates items by whitespace otherwise."""
        items = []
        buf = ""
        depth = 0
        for ch in data:
            if ch == "{":
                depth += 1
                continue
            if ch == "}":
                depth -= 1
                continue
            if ch.isspace() and depth == 0:
                if buf:
                    items.append(buf)
                    buf = ""
                continue
            buf += ch
        if buf:
            items.append(buf)
        return items

    def _on_browse_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Add Backing Track",
            filetypes=[
                ("Audio/video files", "*.flac *.wav *.mp3 *.m4a *.aac *.ogg *.opus "
                                       "*.mp4 *.m4v *.mov *.mkv *.webm *.avi"),
                ("All files", "*.*"),
            ],
            parent=self,
        )
        if path:
            self._set_file_path(path)

    # --- YouTube tab ---

    def _build_youtube_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self._youtube_tab = tab
        self.notebook.add(tab, text="YouTube URL")

        ttk.Label(tab, text="YouTube URL").pack(anchor="w", pady=(0, 4))
        self.youtube_url_var = tk.StringVar()
        entry = ttk.Entry(tab, textvariable=self.youtube_url_var, width=50)
        entry.pack(fill="x")
        entry.bind("<Return>", lambda _e: self._on_add())

    # --- Inspiration tab ---

    def _build_inspiration_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Inspiration")

        def fetch_artists(text: str) -> list[tuple[str, None]]:
            return [(name, None) for name in self._backend.search_inspiration_artists(text)]

        ttk.Label(tab, text="Artist").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.inspiration_artist_field = _AutocompleteEntry(tab, fetch=fetch_artists)
        self.inspiration_artist_field.grid(row=0, column=1, sticky="ew")

        ttk.Label(tab, text="Title").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.inspiration_title_var = tk.StringVar()
        title_entry = ttk.Entry(tab, textvariable=self.inspiration_title_var)
        title_entry.grid(row=1, column=1, sticky="ew")
        tab.columnconfigure(1, weight=1)

        self.inspiration_artist_field.bind_return(lambda _e: self._on_add())
        title_entry.bind("<Return>", lambda _e: self._on_add())

    # --- Inspiration Filter tab ---

    def _build_inspiration_filter_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Inspiration Filter")

        ttk.Label(
            tab, text="Adds a slot to the setlist that draws a random track matching this filter "
                      "every session, instead of one fixed song — the setlist stays as it is, so the "
                      "same slot draws again next session. If a previous draw already has a take from "
                      "a different instrument, it's preferred over a brand new draw, so instruments can "
                      "layer onto the same song. Every take is still saved to the takes archive as usual.",
            foreground="#666666", wraplength=380, justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.filter_fields = FilterCriteriaFields(tab, self._backend, dialog_parent=self, start_row=1)
        self.filter_fields.bind_return(lambda _e: self._on_add())

    # --- add ---

    def _on_window_close(self) -> None:
        if self._working:
            return  # a download/copy in flight would just be orphaned, not stopped
        self.destroy()

    def _on_add(self) -> None:
        if self._working:
            return
        current = self.notebook.tab(self.notebook.select(), "text")
        if current == "File":
            if not self._file_path:
                messagebox.showerror("Cannot add", "Choose a file first.", parent=self)
                return
            file_path = self._file_path
            self._start_add(lambda backend, project: backend.add_local_backing_track(project, file_path))
        elif current == "YouTube URL":
            url = self.youtube_url_var.get().strip()
            if not url:
                messagebox.showerror("Cannot add", "Enter a YouTube URL.", parent=self)
                return
            if not is_youtube_url(url):
                messagebox.showerror("Not a YouTube URL", "Enter a valid YouTube URL.", parent=self)
                return

            def do_youtube(backend: Backend, project: str) -> dict:
                def on_progress(percent: float | None, message: str) -> None:
                    self.after(0, lambda: self._update_progress(percent, message))
                return backend.add_youtube_backing_track(project, url, on_progress=on_progress)

            self._start_add(do_youtube)
        elif current == "Inspiration":
            artist = self.inspiration_artist_field.get().strip()
            title = self.inspiration_title_var.get().strip()
            if not artist and not title:
                messagebox.showerror("Cannot add", "Enter an artist and/or title.", parent=self)
                return

            def do_inspiration(backend: Backend, project: str) -> dict:
                def on_progress(percent: float | None, message: str) -> None:
                    self.after(0, lambda: self._update_progress(percent, message))
                return backend.add_inspiration_backing_track(project, artist, title, on_progress=on_progress)

            self._start_add(do_inspiration)
        else:
            filter_criteria = self.filter_fields.get_criteria()
            if filter_criteria is None:
                return
            label = derive_filter_label(filter_criteria)
            self._start_add(lambda backend, project: backend.add_inspiration_filter_slot(project, label, filter_criteria))

    def _start_add(self, call: Callable[[Backend, str], dict]) -> None:
        self._working = True
        self.add_button.state(["disabled"])
        self.cancel_button.state(["disabled"])
        self.status_var.set("Working...")
        self.progress["value"] = 0
        backend = self._backend
        project_name = self._project_name

        def worker() -> None:
            try:
                call(backend, project_name)
                self.after(0, lambda: self._on_add_done(None))
            except BackendError as e:
                self.after(0, lambda err=str(e): self._on_add_done(err))

        threading.Thread(target=worker, daemon=True).start()

    def _update_progress(self, percent: float | None, message: str) -> None:
        if not self.winfo_exists():
            return
        if percent is not None:
            self.progress["value"] = percent
        self.status_var.set(message)

    def _on_add_done(self, error: str | None) -> None:
        self._working = False
        if not self.winfo_exists():
            if not error:
                self._on_track_added()
            return
        if error:
            self.progress["value"] = 0
            self.status_var.set("")
            self.add_button.state(["!disabled"])
            self.cancel_button.state(["!disabled"])
            messagebox.showerror("Cannot add track", error, parent=self)
            return
        self._on_track_added()
        self.destroy()
