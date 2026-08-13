"""Project, Setlist, and TrackEntry management.

A project is just a setlist file (<projects_dir>/<name>.json) — nothing
session- or file-storage-shaped lives with it. Backing tracks, completed
takes, and recorded sessions all live in the Studio Session Vault instead
(see takeloom/vault.py), shared across every project rather than each
project owning its own copies. That's what lets two different projects
reference the same inspiration-server song without downloading or
recording it twice — see vault.py's inspiration take index.
"""

from __future__ import annotations

import json
import secrets
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .utils import atomic_write_text, ensure_dir, sanitize_filename


@dataclass
class TakeInfo:
    """Reference to a completed take file."""
    instrument: str  # instrument *label* (e.g. "electric-guitar"), not full_name — see TrackEntry.preferred_takes
    take_number: int
    filename: str
    volume: float = 1.0
    # Derived fresh from disk by Project.open() (see _sync_has_video) rather
    # than trusted as persisted truth — self-heals if the video mux failed,
    # was never attempted, or the .mp4 was later removed by hand.
    has_video: bool = False
    # Which physical input (an InputLabel.label — e.g. "guitar-cable", not
    # an instrument identity) actually recorded this take, captured at
    # record time from the session (see backend.py's _SessionEvent/
    # _save_session_log and processing/splicer.py's CompletedTake) rather
    # than derived after the fact from `instrument`/config. An immutable
    # fact about the take itself, so backend.py's analyze_take can use it
    # to precisely scope its comparison even after the take's label has
    # been renamed or removed from config entirely — see that method's
    # docstring.
    input_label: str = ""


@dataclass
class TrackEntry:
    """A single song/track in the setlist.

    A track with is_inspiration_filter=True is a standing "slot" rather
    than a fixed song: backing_track stays "" (there's no file — this
    entry is never itself loaded for playback), and inspiration_filter
    holds the filter criteria (e.g. {"artist": "Miles Davis"} or
    {"genre": "Rock"}) a session draws an actual song from fresh each time
    this slot comes up — see backend.py's _resolve_filter_slot. The
    setlist itself never grows a new entry for a drawn song; which song
    got drawn, and its take, are tracked in the Studio Session Vault's
    shared inspiration-take index (vault.py) instead — keyed by
    inspiration_track_id, not by which project or slot happened to draw
    it, so any project referencing the same song sees the same takes.
    preferred_takes on the filter slot's own entry is therefore always
    empty, which is also what makes it come up as "still needs a take"
    again on every future session.
    """
    name: str
    backing_track: str  # relative path to backing track file
    duration_seconds: float = 0.0
    volume: int = 100  # playback volume percentage
    takes_volume: int = 100  # playback volume for other instruments' takes
    inspiration_track_id: int = 0  # radioserver track ID (0 = local file)
    is_inspiration_filter: bool = False
    inspiration_filter: dict = field(default_factory=dict)
    # Raw inspiration-server search results for inspiration_filter, from
    # the most recent search_tracks_by_filter call made on this slot's
    # behalf (see backend.py's get_filter_slot_previews) — cached here so
    # opening the project again doesn't re-hit the inspiration server just
    # to redisplay the same "next up" previews. Cleared whenever
    # inspiration_filter itself changes (see record.py's _on_edit_filter)
    # so a stale list under new criteria can't linger; empty for an
    # ordinary track or a filter slot never yet previewed.
    cached_matches: list[dict] = field(default_factory=list)
    preferred_takes: dict[str, TakeInfo] = field(default_factory=dict)
    # key = instrument label (not full_name — a Stratocaster and a
    # Telecaster take of the same song share one "electric-guitar" key
    # and one take-number sequence, see processing/splicer.py), value =
    # the preferred take for that label
    source: str = "upload"  # "upload" or "youtube" — only meaningful when
    # inspiration_track_id is 0; see source_label(). Existing entries
    # from before this field existed default to "upload".

    def set_preferred_take(self, instrument: str, take: TakeInfo) -> None:
        self.preferred_takes[instrument] = take

    def get_take_for_instrument(self, instrument: str) -> TakeInfo | None:
        return self.preferred_takes.get(instrument)

    def source_label(self) -> str:
        """Human-readable source: "inspiration" (radioserver-sourced,
        including a filter slot's drawn song), "youtube" (a downloaded
        video), or "upload" (a manually added local file) — used to make
        completed-take filenames explicit about what they were recorded
        against (see utils.take_filename)."""
        if self.inspiration_track_id:
            return "inspiration"
        return self.source

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> TrackEntry:
        takes = {}
        for inst, take_data in data.get("preferred_takes", {}).items():
            takes[inst] = TakeInfo(**take_data)
        return cls(
            name=data["name"],
            backing_track=data["backing_track"],
            duration_seconds=data.get("duration_seconds", 0.0),
            volume=data.get("volume", 100),
            takes_volume=data.get("takes_volume", 100),
            inspiration_track_id=data.get("inspiration_track_id", 0),
            is_inspiration_filter=data.get("is_inspiration_filter", False),
            inspiration_filter=data.get("inspiration_filter", {}),
            cached_matches=data.get("cached_matches", []),
            preferred_takes=takes,
            source=data.get("source", "upload"),
        )


@dataclass
class Setlist:
    """Ordered list of tracks for a project."""
    tracks: list[TrackEntry] = field(default_factory=list)
    backup_server: str = ""

    def add_track(self, track: TrackEntry) -> None:
        self.tracks.append(track)

    def remove_track(self, index: int) -> None:
        if 0 <= index < len(self.tracks):
            self.tracks.pop(index)

    def move_track(self, from_idx: int, to_idx: int) -> None:
        if 0 <= from_idx < len(self.tracks) and 0 <= to_idx < len(self.tracks):
            track = self.tracks.pop(from_idx)
            self.tracks.insert(to_idx, track)

    def to_dict(self) -> dict:
        d: dict = {"tracks": [t.to_dict() for t in self.tracks]}
        if self.backup_server:
            d["backup_server"] = self.backup_server
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Setlist:
        tracks = [TrackEntry.from_dict(t) for t in data.get("tracks", [])]
        return cls(
            tracks=tracks,
            backup_server=data.get("backup_server", ""),
        )


class Project:
    """A recording project: just a setlist file, plus the (shared, vault-
    wide) backing_tracks/completed_takes directories every project reads
    and writes into."""

    def __init__(self, setlist_path: Path, vault_root: Path) -> None:
        self.setlist_path = setlist_path
        self.name = setlist_path.stem
        self.backing_tracks_dir = vault_root / "backing_tracks"
        self.completed_takes_dir = vault_root / "completed_takes"
        self.setlist = Setlist()

    def create(self) -> None:
        """Create a new, empty project: just the setlist file — the
        shared vault directories are ensured on demand by whatever
        actually writes into them first, same as any other project using
        them."""
        ensure_dir(self.setlist_path.parent)
        self.save_setlist()

    @classmethod
    def open(cls, setlist_path: Path, vault_root: Path) -> Project:
        """Open an existing project from its setlist file."""
        proj = cls(setlist_path, vault_root)
        if proj.setlist_path.exists():
            data = json.loads(proj.setlist_path.read_text())
            proj.setlist = Setlist.from_dict(data)
            proj._sync_has_video()
        return proj

    def _sync_has_video(self) -> None:
        """Set each take's has_video flag from whether its companion .mp4
        actually exists in completed_takes_dir, rather than from whatever
        was last persisted — so a take is correctly flagged "audio only"
        whether video was never recorded, muxing failed, or the file was
        since deleted."""
        for track in self.setlist.tracks:
            for take in track.preferred_takes.values():
                video_path = self.completed_takes_dir / (Path(take.filename).stem + ".mp4")
                take.has_video = video_path.exists()

    @classmethod
    def create_new(cls, parent_dir: Path, name: str, vault_root: Path) -> Project:
        """Create a new project (a single <name>.json setlist file) in the
        given directory."""
        safe_name = sanitize_filename(name)
        proj = cls(parent_dir / f"{safe_name}.json", vault_root)
        proj.create()
        return proj

    def save_setlist(self) -> None:
        atomic_write_text(self.setlist_path, json.dumps(self.setlist.to_dict(), indent=2))

    def load_setlist(self) -> None:
        if self.setlist_path.exists():
            data = json.loads(self.setlist_path.read_text())
            self.setlist = Setlist.from_dict(data)

    def add_inspiration_filter_slot(
        self, label: str, filter_criteria: dict, duration_seconds: float = 0.0,
        cached_matches: list[dict] | None = None,
    ) -> TrackEntry:
        """Add a standing "draw a random song from this filter each
        session" slot to the setlist — see TrackEntry's docstring. Unlike
        add_backing_track, there's no file to copy; the slot itself is
        never played directly. `duration_seconds` is the average across
        every currently-matching track (see inspiration.average_duration)
        — the slot has no single fixed song of its own to derive a
        duration from otherwise. `cached_matches`, if the caller already
        queried the inspiration server for this criteria (e.g. to compute
        duration_seconds), seeds TrackEntry.cached_matches so the first
        get_filter_slot_previews call right after doesn't repeat that same
        query."""
        entry = TrackEntry(
            name=label, backing_track="", is_inspiration_filter=True, inspiration_filter=dict(filter_criteria),
            duration_seconds=duration_seconds, cached_matches=list(cached_matches or []),
        )
        self.setlist.add_track(entry)
        self.save_setlist()
        return entry

    def add_backing_track(
        self, source_path: Path, track_name: str | None = None, duration_seconds: float = 0.0,
        source: str = "upload",
    ) -> TrackEntry:
        """Add a backing track file to the project. Copies the file into
        the vault's shared backing_tracks/ (see takeloom/vault.py), unless
        it's already sitting there (a YouTube download's filename already
        embeds the video's globally unique ID — see youtube.py — so it's
        collision-safe as-is). Anything else gets a short random prefix
        added before copying: backing_tracks/ is shared across every
        project now, so two unrelated projects' local uploads could
        otherwise collide on an ordinary filename like "song1.mp3" and
        silently alias each other.

        `source` ("upload" or "youtube") is recorded on the entry — see
        TrackEntry.source_label() — so completed takes recorded against
        it can name explicitly what backing track they came from."""
        ensure_dir(self.backing_tracks_dir)
        if source_path.parent.resolve() == self.backing_tracks_dir.resolve():
            dest = source_path
        else:
            safe_stem = sanitize_filename(source_path.stem) or "track"
            dest = self.backing_tracks_dir / f"{secrets.token_hex(4)}_{safe_stem}{source_path.suffix}"
            shutil.copy2(source_path, dest)
        name = track_name or source_path.stem
        entry = TrackEntry(name=name, backing_track=dest.name, duration_seconds=duration_seconds, source=source)
        self.setlist.add_track(entry)
        self.save_setlist()
        return entry

    @staticmethod
    def list_projects(parent_dir: Path) -> list[Path]:
        """List existing projects (setlist files) in a directory."""
        if not parent_dir.exists():
            return []
        return sorted(p for p in parent_dir.iterdir() if p.is_file() and p.suffix == ".json")
