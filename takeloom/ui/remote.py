"""Remote tab: connect this takeloom instance to another one's hardware/config.

This tab is connect-only. Hosting a server (accepting incoming connections)
is managed exclusively via `takeloom server` on the command line — see
__main__.py's server_command — so there's no "enable remote server" toggle
or authorized-clients list here, only the list of remotes this machine can
connect *to*.

Auth is a pairing-request flow, not a shared secret: connecting to a remote
for the first time sends no token, which prompts the *other* machine's user
(via its own `takeloom server` terminal) to approve or deny the connection.
On approval a token is minted for that client and stored under a
`KnownRemote` entry here; subsequent connections reuse it silently. One
`KnownRemote` can be flagged "always connect" for startup auto-connect (see
app.py's `run()`).
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from ..backend import BackendError
from ..config import KnownRemote, StudioConfig
from ..remote.backend import RemoteBackend
from ..remote.client import RemoteClient
from ..remote.protocol import REMOTE_SERVER_PORT
from .app_state import AppState


def connect_async(
    widget, app_state: AppState, host: str, port: int, token: str,
    on_done=None, on_error=None, on_pending=None,
) -> None:
    """Connect to a remote takeloom instance and swap it into app_state.backend.
    Runs on a background thread; safe to call from the Tk main thread. Shared
    by the Remote tab's Connect button and `takeloom ui --remote=IP`.

    `on_done`, if given, is called with the connected `RemoteClient` — check
    `client.issued_token` to see whether this was a first-time pairing that
    minted a new token. `on_pending`, if given, is called (on the Tk thread)
    if the server reports it's waiting on a human to approve/deny this
    connection — e.g. to switch a "Connecting..." label to "Waiting to be
    authorized...", since that step can take a while."""

    def on_disconnect(reason: str) -> None:
        widget.after(0, lambda: _handle_disconnect(widget, app_state, host, port, reason))

    def on_pending_cb() -> None:
        if on_pending:
            widget.after(0, on_pending)

    def worker() -> None:
        client = RemoteClient(host, port, token, on_disconnect=on_disconnect)
        try:
            client.connect(timeout=6.0, on_pending=on_pending_cb)
            remote_backend = RemoteBackend(client)
            error = None
        except BackendError as e:
            remote_backend = None
            error = str(e)

        def finish() -> None:
            if error:
                if on_error:
                    on_error(error)
                return
            app_state.set_backend(remote_backend, remote_name=remote_backend.hostname())
            if on_done:
                on_done(client)

        widget.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def remember_remote_token(app_state: AppState, host: str, client: RemoteClient) -> None:
    """After a successful connect, persist a newly issued token (if any) onto
    the matching KnownRemote entry in local config, creating one if this was
    a first-time pairing initiated some other way (e.g. `--remote=IP`)."""
    if not client.issued_token:
        return
    backend = app_state.local_backend
    try:
        config = backend.get_config()
    except BackendError:
        return
    remote = next((r for r in config.known_remotes if r.host == host), None)
    if remote is None:
        remote = KnownRemote(host=host)
        config.known_remotes.append(remote)
    remote.token = client.issued_token
    if not remote.label:
        remote.label = client.hostname
    try:
        backend.save_config(config)
    except BackendError:
        pass


def _handle_disconnect(widget, app_state: AppState, host: str, port: int, reason: str) -> None:
    from .app import handle_remote_disconnect
    handle_remote_disconnect(widget.winfo_toplevel(), app_state, host, port, reason)


class _AddRemoteDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, on_add) -> None:
        super().__init__(master)
        self.title("Add Remote")
        self.resizable(False, False)
        self.transient(master)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="IP address / host").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.host_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.host_var, width=28).grid(row=0, column=1, sticky="w")

        ttk.Label(frame, text="Label (optional)").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.label_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.label_var, width=28).grid(row=1, column=1, sticky="w")

        button_row = ttk.Frame(frame)
        button_row.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Add", command=lambda: self._submit(on_add)).pack(side="left")

    def _submit(self, on_add) -> None:
        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror("Cannot add remote", "Enter an IP address or hostname.", parent=self)
            return
        on_add(host, self.label_var.get().strip())
        self.destroy()


class RemoteFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self._local_config: StudioConfig | None = None
        self._remote_items: dict[str, KnownRemote] = {}

        self._build_connect_section()

        self.app_state.add_listener(self._on_app_state_changed)
        self.bind("<Destroy>", self._on_destroy)

        self._refresh_connect_status()
        self._load_local_config()

    def _on_destroy(self, _event: object) -> None:
        self.app_state.remove_listener(self._on_app_state_changed)

    def _on_app_state_changed(self) -> None:
        self._refresh_connect_status()

    def _mutate_config(self, mutate: Callable[[StudioConfig], None]) -> None:
        """Apply `mutate` to a freshly-loaded config — never to the possibly-
        stale `self._local_config` — and persist the result.

        `self._local_config` is only refreshed when this tab is (re)built,
        so it can go stale relative to disk if something else touches the
        same file — most notably a `takeloom server` process pairing a new
        client while this tab is just sitting open. Blindly resaving the
        cached copy in that situation would silently wipe out whatever
        changed. Reading fresh right before every write closes that gap."""
        backend = self.app_state.local_backend
        try:
            config = backend.get_config()
        except BackendError:
            return
        mutate(config)
        try:
            backend.save_config(config)
        except BackendError:
            return
        self._local_config = config
        self._refresh_known_remotes_list()

    # --- connect section ---

    def _build_connect_section(self) -> None:
        frame = ttk.Frame(self)
        frame.pack(fill="x", anchor="w")

        ttk.Label(frame, text="Connect to a Remote Instance", font=("TkDefaultFont", 12, "bold")).pack(
            anchor="w", pady=(0, 8)
        )

        self.remotes_tree = ttk.Treeview(
            frame, columns=("target", "always"), show="tree headings", height=5, selectmode="browse",
        )
        self.remotes_tree.heading("#0", text="Label")
        self.remotes_tree.heading("target", text="Host")
        self.remotes_tree.heading("always", text="Always Connect")
        self.remotes_tree.column("#0", width=160)
        self.remotes_tree.column("target", width=180)
        self.remotes_tree.column("always", width=110, anchor="center")
        self.remotes_tree.pack(fill="x")
        self.remotes_tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_connect_status())

        button_row = ttk.Frame(frame)
        button_row.pack(anchor="w", pady=(8, 0))
        ttk.Button(button_row, text="Add Remote", command=self._on_add_remote).pack(side="left")
        self.connect_button = ttk.Button(button_row, text="Connect", command=self._on_connect)
        self.connect_button.pack(side="left", padx=(8, 0))
        self.disconnect_button = ttk.Button(button_row, text="Disconnect", command=self._on_disconnect_click)
        self.disconnect_button.pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="Forget", command=self._on_forget_remote).pack(side="left", padx=(8, 0))

        self.always_connect_var = tk.BooleanVar(value=False)
        self.always_connect_check = ttk.Checkbutton(
            frame, text="Always connect to selected remote on startup",
            variable=self.always_connect_var, command=self._on_toggle_always_connect,
        )
        self.always_connect_check.pack(anchor="w", pady=(8, 0))

        self.connect_status_var = tk.StringVar(value="Not connected.")
        ttk.Label(frame, textvariable=self.connect_status_var, foreground="#2a6db0").pack(anchor="w", pady=(8, 0))

        ttk.Label(
            frame,
            text=f"To host a server other instances can connect to, run `takeloom server` "
                 f"on that machine (port {REMOTE_SERVER_PORT}, fixed).",
            foreground="#666666", wraplength=420, justify="left",
        ).pack(anchor="w", pady=(12, 0))

    def _selected_remote(self) -> KnownRemote | None:
        sel = self.remotes_tree.selection()
        if not sel:
            return None
        return self._remote_items.get(sel[0])

    def _reselect(self, remote: KnownRemote | None) -> None:
        if remote is None:
            return
        if self.remotes_tree.exists(remote.host):
            self.remotes_tree.selection_set(remote.host)

    def _refresh_known_remotes_list(self) -> None:
        previously_selected = self._selected_remote()
        self.remotes_tree.delete(*self.remotes_tree.get_children())
        self._remote_items = {}
        if self._local_config is None:
            self._refresh_connect_status()
            return
        for remote in self._local_config.known_remotes:
            self.remotes_tree.insert(
                "", "end", iid=remote.host, text=remote.label or "(unpaired)",
                values=(remote.host, "✓" if remote.always_connect else ""),
            )
            self._remote_items[remote.host] = remote
        # Reload of local config replaces every KnownRemote instance, so the
        # previous selection (by object identity) no longer resolves — reselect
        # by host instead, so a Connect click doesn't silently lose the row
        # the user had selected (e.g. mid "Always connect" toggle).
        self._reselect(previously_selected)
        self._refresh_connect_status()

    def _refresh_connect_status(self) -> None:
        remote = self._selected_remote()
        if self.app_state.backend.is_remote():
            self.connect_status_var.set(f"Connected: {self.app_state.remote_name}")
            self.connect_button.state(["disabled"])
            self.disconnect_button.state(["!disabled"])
        else:
            self.connect_status_var.set("Not connected.")
            self.disconnect_button.state(["disabled"])
            self.connect_button.state(["!disabled"] if remote is not None else ["disabled"])
        self.always_connect_var.set(bool(remote and remote.always_connect))
        self.always_connect_check.state(["!disabled"] if remote is not None else ["disabled"])

    def _on_add_remote(self) -> None:
        def on_add(host: str, label: str) -> None:
            if self._local_config is None:
                return
            # Reuse an existing entry for this host rather than appending a
            # second, unpaired one — a duplicate here is how a previously
            # paired remote could end up stuck re-pairing on every connect
            # (see _dedupe_known_remotes in config.py).
            def mutate(config: StudioConfig) -> None:
                existing = next((r for r in config.known_remotes if r.host == host), None)
                if existing is not None:
                    if label:
                        existing.label = label
                else:
                    config.known_remotes.append(KnownRemote(host=host, label=label))
            self._mutate_config(mutate)
            self._reselect(next((r for r in self._local_config.known_remotes if r.host == host), None))

        _AddRemoteDialog(self, on_add)

    def _on_forget_remote(self) -> None:
        remote = self._selected_remote()
        if remote is None or self._local_config is None:
            return
        if self.app_state.backend.is_remote() and self.app_state.remote_name and remote.label == self.app_state.remote_name:
            if not messagebox.askyesno("Forget remote", "This is the currently connected remote. Forget it anyway?"):
                return
        host = remote.host

        def mutate(config: StudioConfig) -> None:
            config.known_remotes = [r for r in config.known_remotes if r.host != host]
        self._mutate_config(mutate)

    def _on_toggle_always_connect(self) -> None:
        remote = self._selected_remote()
        if remote is None or self._local_config is None:
            self.always_connect_var.set(False)
            return
        new_value = self.always_connect_var.get()
        host = remote.host

        def mutate(config: StudioConfig) -> None:
            for r in config.known_remotes:
                r.always_connect = (r.host == host and new_value)
        self._mutate_config(mutate)

    def _on_connect(self) -> None:
        if self.app_state.recording_active:
            messagebox.showerror("Cannot connect", "Stop the current recording first.")
            return
        remote = self._selected_remote()
        if remote is None:
            messagebox.showerror("Cannot connect", "Select a remote to connect to, or add one first.")
            return

        self.connect_button.state(["disabled"])
        self.connect_status_var.set(f"Connecting to {remote.host}...")

        def on_pending() -> None:
            self.connect_status_var.set(f"Waiting to be authorized by {remote.host}...")

        def on_done(client: RemoteClient) -> None:
            remember_remote_token(self.app_state, remote.host, client)
            self._load_local_config()
            self._refresh_connect_status()

        def on_error(error: str) -> None:
            self._refresh_connect_status()
            messagebox.showerror("Connect failed", error)

        connect_async(
            self, self.app_state, remote.host, REMOTE_SERVER_PORT, remote.token,
            on_done=on_done, on_error=on_error, on_pending=on_pending,
        )

    def _on_disconnect_click(self) -> None:
        if self.app_state.recording_active:
            messagebox.showerror("Cannot disconnect", "Stop the current recording first.")
            return
        if not self.app_state.backend.is_remote():
            return
        self.app_state.set_backend(self.app_state.local_backend)
        # Deliberately back in local mode — resume monitoring this
        # machine's own hardware (unlike app.py's handle_remote_disconnect,
        # which only drops to local momentarily while reconnecting).
        self.app_state.start_local_monitoring()
        self._refresh_connect_status()

    def _load_local_config(self) -> None:
        backend = self.app_state.local_backend

        def worker() -> None:
            try:
                config, error = backend.get_config(), None
            except BackendError as e:
                config, error = None, str(e)
            self.after(0, lambda: self._on_local_config_loaded(config, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_local_config_loaded(self, config: StudioConfig | None, error: str | None) -> None:
        if error or config is None:
            return
        self._local_config = config
        self._refresh_known_remotes_list()
