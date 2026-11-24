"""The Completed Takes page's data: every completed take across the vault,
grouped by song — what takeloom's old Completed Takes tab showed (its
Backend.list_completed_takes), worked out here from the vault's own copies
of every project setlist (projects/*.json) and the shared inspiration-take
index (inspiration_takes.json), which takeloom keeps mirrored up.

Setlists are still takeloom's to write: the studio machine owns them and
uploads them on every sync. So a change made here — a song's preferred
take for an instrument, or its backing-track trim — is queued in
setlist_edits.json instead, and shown at once by applying the queue over
the vault's copies whenever the page is built (overlay). takeloom applies
each edit to its own setlists on its next sync (and before a session
opens), uploads them, then acknowledges the edit (POST /api/setlist-
edits/ack), which takes it off the queue: from then on the uploaded
setlists already say the same thing.

Song mixes (mixes/<song>.json) and exported mixes (final_mixes/<song>.mp3)
are this server's own now; takeloom no longer writes either. So are songs'
working titles (working_titles.json, {song name: working title}): what a
song's called on YouTube — the title a session's upload is given, and the
name in the footer drawn over its video (footer.py, youtube.py) — while
the setlists keep the name it was recorded under.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

import numpy as np

from .store import Vault, _atomic_write

EDITS_FILE = "setlist_edits.json"
WORKING_TITLES_FILE = "working_titles.json"
TAKE_AUDIO_EXTS = (".mp3", ".flac")
# Backing-track formats sent to the browser as they are; anything else (an
# .opus, say) is decoded to FLAC first.
_BROWSER_AUDIO = (".mp3", ".flac", ".wav", ".m4a")
_TAKE_RE = re.compile(r"^(?P<label>(?:(?! - ).)+) - take(?P<n>\d+)(?: \[(?P<tag>.*)\])?$")
_YOUTUBE_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{6,15})\]|_([A-Za-z0-9_-]{6,15})$")
_INSPIRATION_STEM_RE = re.compile(r"^inspiration_(\d+)$")
_UPLOAD_PREFIX_RE = re.compile(r"^([0-9a-f]{8})_")
SAMPLE_RATE = 48000
MAX_GAIN = 2.0


class EditError(ValueError):
    pass


# --- takeloom's naming, copied (keep in step with takeloom/utils.py) ---

def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def _backing_track_id(source: str, backing_track: str) -> str:
    stem = Path(backing_track).stem if backing_track else "unknown"
    if source == "inspiration":
        m = _INSPIRATION_STEM_RE.match(stem)
        if m:
            return m.group(1)
    elif source == "youtube":
        m = _YOUTUBE_ID_RE.search(stem)
        if m:
            return m.group(1) or m.group(2)
    elif source == "upload":
        m = _UPLOAD_PREFIX_RE.match(stem)
        if m:
            return m.group(1)
    return stem


def source_label(entry: dict) -> str:
    return "inspiration" if entry.get("inspiration_track_id") else (entry.get("source") or "upload")


def inspiration_song_name(match: dict) -> str:
    """takeloom/inspiration.py's build_inspiration_track_entry's name."""
    year = match.get("year", "")
    return f"{match.get('artist', 'Unknown')} - {match.get('title', 'Unknown')}{f' ({year})' if year else ''}"


def _mix_path(root: Path, track_name: str) -> Path:
    return root / "mixes" / f"{sanitize_filename(track_name) or 'untitled'}.json"


def _ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise EditError(f"Couldn't read the length of {path.name}: {result.stderr.strip()[-200:]}") from None


def _decode(path: Path) -> np.ndarray:
    """`path` as float32 (frames, 2) at SAMPLE_RATE; mono goes to both channels."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
         "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True,
    )
    channels = 1 if probe.stdout.strip() == "1" else 2
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", str(channels), "-ar", str(SAMPLE_RATE),
         "-f", "f32le", "-"], capture_output=True,
    )
    if result.returncode != 0:
        raise EditError(f"Couldn't decode {path.name}: {result.stderr.decode(errors='replace').strip()[-200:]}")
    data = np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, channels)
    return np.repeat(data, 2, axis=1) if channels == 1 else data


def _encode(data: np.ndarray, out: Path, codec: list[str]) -> None:
    tmp = out.with_name(f".partial-{out.name}")
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", str(data.shape[1]),
         "-i", "-", *codec, str(tmp)], input=np.ascontiguousarray(data, dtype=np.float32).tobytes(),
        capture_output=True,
    )
    if result.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise EditError(f"Couldn't encode {out.name}: {result.stderr.decode(errors='replace').strip()[-200:]}")
    tmp.replace(out)


def working_titles(root: Path) -> dict[str, str]:
    """{song name: working title} — only songs that have one."""
    try:
        data = json.loads((root / WORKING_TITLES_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v).strip() for k, v in data.items() if isinstance(v, str) and v.strip()}


class Library:
    def __init__(self, vault: Vault, cache_dir: Path, take_videos, log) -> None:
        self.vault = vault
        self.cache_dir = cache_dir
        self.take_videos = take_videos  # for each take's lossless original (takes.py's original_for)
        self._log = log
        self._lock = threading.Lock()  # setlist_edits.json, mixes, the backing cache
        self._exports: dict[str, dict] = {}  # song -> {"state", "error"?, "notes"?, "url"?}
        self._export_lock = threading.Lock()

    # --- records: every setlist entry and inspiration-index entry ---

    def _records(self) -> list[dict]:
        """Each record a take can be filed on, with the queued edits applied:
        {"entry": <TrackEntry dict>, "project": <name>|None}."""
        root = self.vault.root
        records = []
        projects = root / "projects"
        if projects.is_dir():
            for path in sorted(projects.glob("*.json")):
                if path.name.startswith("."):
                    continue
                try:
                    data = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                for track in data.get("tracks") or []:
                    if isinstance(track, dict) and track.get("name"):
                        records.append({"entry": track, "project": path.stem})
        try:
            index = json.loads((root / "inspiration_takes.json").read_text())
        except (OSError, ValueError):
            index = {}
        for entry in (index.values() if isinstance(index, dict) else []):
            if isinstance(entry, dict) and entry.get("name"):
                records.append({"entry": entry, "project": None})
        files = self._take_files()
        for edit in self.edits():
            try:
                self._apply(records, edit, files)
            except EditError:
                pass  # takeloom will report it too, and drop it
        return records

    def _take_files(self) -> dict[str, float]:
        completed = self.vault.root / "completed_takes"
        files = {}
        if completed.is_dir():
            for f in completed.iterdir():
                if f.suffix.lower() in TAKE_AUDIO_EXTS and not f.name.startswith("."):
                    files[f.name] = f.stat().st_mtime
        return files

    def _scan(self, names, entry: dict) -> dict[str, list[dict]]:
        """takeloom/backend.py's _scan_song_take_files (keep in step)."""
        completed = self.vault.root / "completed_takes"
        prefix = f"{sanitize_filename(entry['name'])} - "
        source = source_label(entry)
        expected_tag = f"{source}:{sanitize_filename(_backing_track_id(source, entry.get('backing_track') or ''))}"
        found: dict[str, list[dict]] = {}
        for name in names:
            stem, ext = Path(name).stem, Path(name).suffix.lower()
            if ext not in TAKE_AUDIO_EXTS or not stem.startswith(prefix):
                continue
            m = _TAKE_RE.match(stem[len(prefix):])
            if m is None or (m.group("tag") is not None and m.group("tag") != expected_tag):
                continue
            found.setdefault(m.group("label"), []).append({
                "take_number": int(m.group("n")), "filename": name,
                "has_video": (completed / f"{stem}.mp4").exists() or (completed / f"{stem}.video.json").exists(),
                "has_midi": (completed / f"{stem}.mid").exists(),
            })
        for takes in found.values():
            takes.sort(key=lambda t: t["take_number"])
        return found

    @staticmethod
    def _matching(records: list[dict], take_filename: str) -> list[dict]:
        return [r for r in records if any(
            isinstance(t, dict) and t.get("filename") == take_filename
            for t in (r["entry"].get("preferred_takes") or {}).values()
        )]

    def _apply(self, records: list[dict], edit: dict, files: dict) -> None:
        """One queued edit over `records`, as takeloom's set_preferred_take /
        edit_backing_track would make it."""
        matched = self._matching(records, str(edit.get("take_filename") or ""))
        if not matched:
            raise EditError(f"No song has take {edit.get('take_filename')!r} any more.")
        if edit.get("type") == "preferred_take":
            label, new = str(edit.get("instrument") or ""), edit.get("new_filename")
            take = None
            if new:
                match = next((t for t in self._scan([new] if new in files else [], matched[0]["entry"]).get(label, [])
                              if t["filename"] == new), None)
                if match is None:
                    raise EditError(f"{new!r} isn't a {label} take of {matched[0]['entry']['name']!r}.")
                take = {"instrument": label, "take_number": match["take_number"], "filename": new, "volume": 1.0,
                        "has_video": match["has_video"], "has_midi": match["has_midi"], "input_label": "",
                        "source": None}
            for r in matched:
                takes = r["entry"].setdefault("preferred_takes", {})
                if take is None:
                    takes.pop(label, None)
                else:
                    takes[label] = dict(take)
        elif edit.get("type") == "trim":
            for r in matched:
                r["entry"]["trim_start_seconds"] = float(edit.get("trim_start_seconds") or 0.0)
                r["entry"]["trim_end_seconds"] = float(edit.get("trim_end_seconds") or 0.0)
        else:
            raise EditError(f"Unknown edit {edit.get('type')!r}")

    # --- the page's data ---

    def songs(self) -> tuple[list[dict], dict[str, list[str]]]:
        """(songs, projects): every song with a take, sorted by name —
        {"name", "backing_source", "backing_track", "trim_start_seconds",
        "trim_end_seconds", "takes": [{"instrument", "filename",
        "take_number", "has_video", "recorded_at", "alternates": [...]}],
        "unpreferred": {label: [take, ...]}, "mix": saved mix or None,
        "working_title": "" if none} —
        and each project's song names (for filtering by project)."""
        records = self._records()
        files = self._take_files()
        names = list(files)
        songs: dict[str, dict] = {}
        scans: dict[tuple, dict] = {}
        for r in records:
            entry = r["entry"]
            preferred = {label: t for label, t in (entry.get("preferred_takes") or {}).items()
                         if isinstance(t, dict) and t.get("filename")}
            if not preferred:
                continue
            song = songs.setdefault(entry["name"], {
                "name": entry["name"], "backing_source": source_label(entry),
                "backing_track": entry.get("backing_track") or "",
                "trim_start_seconds": float(entry.get("trim_start_seconds") or 0.0),
                "trim_end_seconds": float(entry.get("trim_end_seconds") or 0.0),
                "takes": {}, "scan": {},
            })
            key = (entry["name"], source_label(entry), entry.get("backing_track") or "")
            if key not in scans:
                scans[key] = self._scan(names, entry)
            for label, take in preferred.items():
                alternates = {t["filename"]: t for t in scans[key].get(label, [])}
                alternates.setdefault(take["filename"], {
                    "take_number": take.get("take_number", 0), "filename": take["filename"],
                    "has_video": bool(take.get("has_video")), "has_midi": bool(take.get("has_midi")),
                })
                song["takes"][take["filename"]] = {
                    "instrument": label, "filename": take["filename"], "take_number": take.get("take_number", 0),
                    "has_video": bool(take.get("has_video")), "recorded_at": files.get(take["filename"]),
                    "available": take["filename"] in files,
                    "alternates": sorted(alternates.values(), key=lambda t: t["take_number"]),
                }
            for label, takes in scans[key].items():
                song["scan"].setdefault(label, takes)
        titles = working_titles(self.vault.root)
        out = []
        for song in sorted(songs.values(), key=lambda s: s["name"].lower()):
            takes = sorted(song.pop("takes").values(), key=lambda t: t["instrument"])
            labels = {t["instrument"] for t in takes}
            song["takes"] = takes
            song["unpreferred"] = {label: t for label, t in sorted(song.pop("scan").items()) if label not in labels}
            song["mix"] = self.mix(song["name"])
            song["working_title"] = titles.get(song["name"], "")
            out.append(song)
        projects: dict[str, list[str]] = {}
        for r in records:
            if r["project"] is None:
                continue
            entry = r["entry"]
            names_ = projects.setdefault(r["project"], [])
            if entry.get("is_inspiration_filter"):
                names_.extend(inspiration_song_name(m) for m in entry.get("song_set") or [] if isinstance(m, dict))
            else:
                names_.append(entry["name"])
        return out, projects

    # --- queued setlist edits ---

    def edits(self) -> list[dict]:
        try:
            data = json.loads((self.vault.root / EDITS_FILE).read_text())
        except (OSError, ValueError):
            return []
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

    def _write_edits(self, edits: list[dict]) -> None:
        _atomic_write(self.vault.root / EDITS_FILE, json.dumps(edits, indent=2).encode())

    def queue(self, edit: dict) -> dict:
        """Check `edit` against the setlists as they'd stand with the queue
        applied, then queue it — after every edit already waiting, which
        takeloom applies in the same order."""
        with self._lock:
            records = self._records()
            self._apply(records, edit, self._take_files())  # raises EditError if it can't apply
            song = self._matching(records, str(edit.get("take_filename") or ""))
            edit = {**edit, "id": uuid.uuid4().hex, "song": song[0]["entry"]["name"] if song else "",
                    "queued_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
            self._write_edits(self.edits() + [edit])
            return edit

    def ack(self, ids: list[str]) -> int:
        with self._lock:
            edits = self.edits()
            kept = [e for e in edits if e.get("id") not in set(ids)]
            if len(kept) != len(edits):
                self._write_edits(kept)
            return len(edits) - len(kept)

    def set_preferred_take(self, take_filename: str, instrument: str, new_filename: str | None) -> dict:
        if not take_filename or not instrument:
            raise EditError("Missing take or instrument.")
        return self.queue({"type": "preferred_take", "take_filename": take_filename, "instrument": instrument,
                           "new_filename": new_filename or None})

    def set_trim(self, take_filename: str, start: float, end: float) -> dict:
        if start < 0 or end < 0:
            raise EditError("Trim amounts can't be negative.")
        records = self._matching(self._records(), take_filename)
        if not records:
            raise EditError(f"No song has take {take_filename!r}.")
        backing = self._backing_source(records[0]["entry"].get("backing_track") or "")
        full = _ffprobe_duration(backing)
        if start + end >= full:
            raise EditError(f"Trimming {start + end:.1f}s leaves nothing of a {full:.1f}s backing track.")
        return self.queue({"type": "trim", "take_filename": take_filename,
                           "trim_start_seconds": round(start, 3), "trim_end_seconds": round(end, 3)})

    # --- mixes ---

    def mix(self, track_name: str) -> dict | None:
        try:
            data = json.loads(_mix_path(self.vault.root, track_name).read_text())
        except (OSError, ValueError):
            return None
        return {
            "volumes": {str(k): float(v) for k, v in (data.get("volumes") or {}).items()},
            "muted": [str(m) for m in data.get("muted") or []],
            "saved_at": data.get("saved_at"),
        }

    def save_mix(self, track_name: str, volumes: dict, muted: list) -> dict:
        """vault.py's save_song_mix's file (keep in step)."""
        if not track_name:
            raise EditError("Can't save a mix without a song name.")
        mix = {
            "track_name": track_name,
            "volumes": {str(k): min(MAX_GAIN, max(0.0, float(v))) for k, v in volumes.items()},
            "muted": sorted(str(m) for m in muted),
            "saved_at": time.time(),
        }
        path = _mix_path(self.vault.root, track_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            _atomic_write(path, json.dumps(mix, indent=2).encode())
        return mix

    # --- working titles ---

    def set_working_title(self, track_name: str, title: str) -> str:
        """Set (or, with "", clear) a song's working title."""
        if not track_name:
            raise EditError("Can't give a working title without a song name.")
        title = " ".join(title.split())[:100]  # YouTube's title limit
        with self._lock:
            titles = working_titles(self.vault.root)
            if title:
                titles[track_name] = title
            else:
                titles.pop(track_name, None)
            _atomic_write(self.vault.root / WORKING_TITLES_FILE,
                          json.dumps(dict(sorted(titles.items())), indent=2).encode())
        return title

    # --- backing tracks, as heard ---

    def _backing_source(self, backing_track: str) -> Path:
        if not backing_track or "/" in backing_track or "\\" in backing_track or backing_track.startswith("."):
            raise EditError("This song has no backing track.")
        path = self.vault.root / "backing_tracks" / backing_track
        if not path.is_file():
            raise EditError(f"backing_tracks/{backing_track} isn't in the vault.")
        return path

    def tuning(self, backing_track: str) -> float:
        try:
            return float(json.loads((self.vault.root / "backing_tuning.json").read_text()).get(backing_track, 0.0))
        except (OSError, ValueError, TypeError, AttributeError):
            return 0.0

    def backing_playback(self, backing_track: str) -> Path:
        """The backing track as a session plays it: with its saved pitch
        correction (pitch_shift.py), in a format browsers decode. Cached."""
        path = self._backing_source(backing_track)
        cents = self.tuning(backing_track)
        if not cents and path.suffix.lower() in _BROWSER_AUDIO:
            return path
        st = path.stat()
        out = self.cache_dir / f"{path.stem}.{st.st_size}.{st.st_mtime_ns}.{cents:+.2f}.flac"
        with self._lock:
            if out.exists():
                return out
            out.parent.mkdir(parents=True, exist_ok=True)
            data = _decode(path)
            if cents:
                from .pitch_shift import render
                data = render(data, SAMPLE_RATE, cents).astype(np.float32)
            _encode(data, out, ["-c:a", "flac"])
        return out

    # --- export ---

    def export_status(self, track_name: str) -> dict:
        with self._export_lock:
            status = dict(self._exports.get(track_name) or {})
        if not status:
            final = self.vault.root / "final_mixes" / f"{sanitize_filename(track_name) or 'untitled'}.mp3"
            status = {"state": "done", "url": f"final_mixes/{final.name}"} if final.is_file() else {"state": "none"}
        return status

    def export(self, track_name: str, tracks: list[dict], trim_start: float, trim_end: float) -> dict:
        """Render what the mixer's playing — `tracks` [{"filename", "label",
        "gain"} | {"backing": true, "backing_track", "gain"}] and the trim —
        to final_mixes/<song>.mp3, from each take's lossless original (as
        takeloom's Backend.export_song_mix did), in the background."""
        if not track_name:
            raise EditError("Can't export a mix without a song name.")
        with self._export_lock:
            if (self._exports.get(track_name) or {}).get("state") == "exporting":
                return dict(self._exports[track_name])
            self._exports[track_name] = {"state": "exporting"}

        def run() -> None:
            try:
                result = self._export(track_name, tracks, trim_start, trim_end)
                status = {"state": "done", **result}
            except Exception as e:  # noqa: BLE001 — shown on the page
                status = {"state": "failed", "error": f"{e}" if isinstance(e, EditError) else f"{type(e).__name__}: {e}"}
            with self._export_lock:
                self._exports[track_name] = status
            self._log(f"Export of {track_name!r}: {status['state']}" + (f" ({status.get('error')})" if status.get("error") else ""))

        threading.Thread(target=run, daemon=True, name="mix-export").start()
        return {"state": "exporting"}

    def _export(self, track_name: str, tracks: list[dict], trim_start: float, trim_end: float) -> dict:
        notes = []
        mixed = []
        for track in tracks:
            gain = min(MAX_GAIN, max(0.0, float(track.get("gain", 1.0))))
            if gain <= 0:
                continue
            if track.get("backing"):
                path = self.backing_playback(str(track.get("backing_track") or ""))
            else:
                filename, label = str(track.get("filename") or ""), str(track.get("label") or "")
                if not filename or "/" in filename or filename.startswith("."):
                    raise EditError(f"Bad take name {filename!r}")
                try:
                    path = self.take_videos.original_for(filename)
                except Exception as e:  # noqa: BLE001 — TakeVideoError: fall back to the .mp3
                    notes.append(f"{label}: mixed from its MP3 ({e})")
                    path = self.vault.root / "completed_takes" / filename
                    if not path.is_file():
                        raise EditError(f"completed_takes/{filename} isn't in the vault.") from None
            mixed.append((_decode(path), gain))
        if not mixed:
            raise EditError(f"Nothing to export for {track_name!r}: every track is muted.")
        length = max(len(d) for d, _ in mixed)
        out = np.zeros((length, 2), dtype=np.float32)
        for data, gain in mixed:
            out[:len(data)] += data[:, :2] * gain
        start = min(length, int(round(trim_start * SAMPLE_RATE)))
        end = max(start, length - int(round(trim_end * SAMPLE_RATE)))
        final_dir = self.vault.root / "final_mixes"
        final_dir.mkdir(parents=True, exist_ok=True)
        final = final_dir / f"{sanitize_filename(track_name) or 'untitled'}.mp3"
        _encode(np.clip(out[start:end], -1.0, 1.0), final, ["-c:a", "libmp3lame", "-q:a", "2"])
        return {"url": f"final_mixes/{final.name}", "notes": notes}

