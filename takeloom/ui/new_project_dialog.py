"""New Project dialog: just a name entry that creates a blank project.
Backing tracks are added afterward via "+ Add to Setlist" (see
`add_to_setlist_dialog.py`).
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from ..backend import Backend, BackendError


class NewProjectDialog(tk.Toplevel):
    """Popup for creating a blank project: just a name."""

    def __init__(self, master: tk.Misc, backend: Backend, on_created: Callable[[str], None]) -> None:
        super().__init__(master)
        self.title("New Setlist")
        self.resizable(False, False)
        self.transient(master)

        self._backend = backend
        self._on_created = on_created

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Setlist name").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(frame, textvariable=self.name_var, width=40)
        self.name_entry.grid(row=0, column=1, sticky="w")
        self.name_entry.focus_set()
        self.name_entry.bind("<Return>", lambda _e: self._on_create())

        footer = ttk.Frame(frame)
        footer.grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self.destroy)
        self.cancel_button.pack(side="left", padx=(0, 8))
        self.create_button = ttk.Button(footer, text="Create", command=self._on_create)
        self.create_button.pack(side="left")

    def _on_create(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Cannot create project", "Enter a project name.", parent=self)
            return

        self.create_button.state(["disabled"])
        self.cancel_button.state(["disabled"])
        self.name_entry.state(["disabled"])

        backend = self._backend

        def worker() -> None:
            try:
                project_name = backend.create_project(name)
            except BackendError as e:
                self.after(0, lambda err=str(e): self._on_create_failed(err))
                return
            self.after(0, lambda: self._on_create_finished(project_name))

        threading.Thread(target=worker, daemon=True).start()

    def _on_create_failed(self, error: str) -> None:
        if not self.winfo_exists():
            return
        messagebox.showerror("Cannot create project", error, parent=self)
        self.create_button.state(["!disabled"])
        self.cancel_button.state(["!disabled"])
        self.name_entry.state(["!disabled"])

    def _on_create_finished(self, project_name: str) -> None:
        self._on_created(project_name)
        if self.winfo_exists():
            self.destroy()
