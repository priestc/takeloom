"""Post-session processing: replay a session's event log to find completed
takes, clip them (and their videos) out of the continuous session recording,
and update the setlist.

This is the deferred half of the session-centric recording model (see the
recording section of backend.py): while a session is live, nothing is
finalized — the backend just captures one continuous audio stream (and,
with a camera, one continuous video) and appends events to the log. Once
the session ends, process_session() does all the heavy lifting here, off
the hot path.

Event vocabulary (each event carries `frame`, a position on the session
audio's timeline, plus `track_index`/`track_name`):

- record_start   — a track's backing started playing from 0:00
- back_to_start  — playing backing sent back to 0:00; the play-through in
                   progress is abandoned, a new one begins at this frame
- song_end       — backing reached its natural end: the play-through in
                   progress IS a completed take
- track_skipped  — playing backing abandoned (Next, or loading another
                   track over it): not a take, unless already long enough
- song_stopped   — playing backing cut off by the session ending: same
                   keep rule as track_skipped
- track_loaded / session_start / session_end / volume events — bookkeeping

Any play-through that reaches song_end is a completed take, no matter how
many back_to_starts came before it. An abandoned play-through is normally
discarded, except when it already ran past MIN_KEPT_TAKE_SECONDS — losing
that much performance to a slip of the finger is worse than an extra
unwanted take in the list.

A setlist "filter slot" (TrackEntry.is_inspiration_filter) complicates
`track_index` slightly: it still points at the slot's own (permanent)
position in the setlist, but the take actually belongs to whichever song
backend.py's _resolve_filter_slot happened to draw for it that session —
never a top-level entry of its own. That's why every take here is named
from the logged `track_name` (the drawn song's name, for a filter slot)
rather than looked up from the setlist entry at track_index (the slot's
own name) — and why a filter slot's take is recorded into the shared
vault-wide inspiration-take index (vault.py, keyed by the drawn song's
inspiration_track_id, from the session log's filter_slot_draws) instead
of the slot's own preferred_takes: the slot itself must stay "always
needs a take" so it keeps getting redrawn, while the shared index is
what lets a later session — in this project or any other — recognize
and prefer a song other instruments have already recorded on. A regular
(non-filter) inspiration-sourced track's take is recorded into both: its
own preferred_takes (as always) and the shared index, so other projects
referencing the same song can find it too.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import soundfile as sf

from ..config import StudioConfig
from ..project import Project, TakeInfo
from ..utils import atomic_write_text, take_filename, next_take_number, ensure_dir
from ..vault import record_inspiration_take, vault_root

# An abandoned (skipped/stopped) play-through this long is kept as a take
# anyway — see module docstring.
MIN_KEPT_TAKE_SECONDS = 10 * 60


@dataclass
class CompletedTake:
    """A take segment identified from the session log, in session-audio frames."""
    track_index: int
    track_name: str
    start_frame: int
    end_frame: int
    start_wall_time: str  # wall_time of the segment's record_start/back_to_start
    # Whichever instrument was active when the segment started (its
    # record_start/back_to_start event's own instrument fields — see
    # backend.py's _SessionEvent) — per-take rather than read once from
    # the session log's top-level instrument/instrument_label fields, so
    # a future session spanning more than one instrument (mid-session
    # auto-detect switching) files each take under the instrument
    # actually active when it was recorded. `instrument` is the
    # Instrument's full_name (its only identifying field — see
    # config.py's Instrument).
    instrument: str = ""
    instrument_label: str = ""
    # Which physical input (an InputLabel.label) actually recorded this
    # take — same per-take reasoning as instrument/instrument_label above.
    # Carried onto the resulting TakeInfo (see process_session) so
    # backend.py's analyze_take can precisely scope its comparison even
    # after the take's label has since been renamed or removed from
    # config — see that method's docstring.
    input_label: str = ""


def parse_session_log(data: dict) -> list[CompletedTake]:
    """Identify completed takes from a session log's events — see the
    module docstring for the rules."""
    sample_rate = data.get("sample_rate") or 48000
    completed: list[CompletedTake] = []

    start_frame: int | None = None
    track_index = 0
    track_name = ""
    start_wall = ""
    instrument = ""
    instrument_label = ""
    input_label = ""

    def close_segment(end_frame: int | None, natural_end: bool) -> None:
        nonlocal start_frame
        if start_frame is None or end_frame is None:
            start_frame = None
            return
        long_enough = (end_frame - start_frame) / sample_rate >= MIN_KEPT_TAKE_SECONDS
        if natural_end or long_enough:
            completed.append(CompletedTake(
                track_index=track_index, track_name=track_name,
                start_frame=start_frame, end_frame=end_frame,
                start_wall_time=start_wall,
                instrument=instrument, instrument_label=instrument_label, input_label=input_label,
            ))
        start_frame = None

    for event in data.get("events", []):
        etype = event.get("event_type")
        frame = event.get("frame")

        if etype in ("record_start", "back_to_start"):
            # back_to_start both abandons the play-through in progress
            # (kept only if it ran long enough) and starts a new one.
            close_segment(frame, natural_end=False)
            start_frame = frame
            track_index = event.get("track_index", 0)
            track_name = event.get("track_name", "")
            start_wall = event.get("wall_time", "")
            instrument = event.get("instrument", "")
            instrument_label = event.get("instrument_label", "")
            input_label = event.get("input_label", "")
        elif etype == "song_end":
            close_segment(frame, natural_end=True)
        elif etype in ("track_skipped", "song_stopped", "track_loaded", "session_end"):
            close_segment(frame, natural_end=False)

    return completed


def _copy_flac_segment(src: sf.SoundFile, start: int, end: int, out_path: Path) -> None:
    """Write src's [start, end) frame range to out_path, block-wise — a
    session recording can be hours long, so it's never loaded whole."""
    with sf.SoundFile(
        str(out_path), mode="w", samplerate=src.samplerate,
        channels=src.channels, format="FLAC", subtype="PCM_16",
    ) as out:
        src.seek(start)
        remaining = end - start
        while remaining > 0:
            block = src.read(min(remaining, 1 << 20), dtype="float32", always_2d=True)
            if len(block) == 0:
                break
            out.write(block)
            remaining -= len(block)


def process_session(session_dir: Path, config: StudioConfig) -> str:
    """Turn a finished session directory (session.flac + session_log.json,
    plus the raw video/mix pair when a camera ran) into completed take files
    and setlist updates. Returns a one-line human-readable summary.

    Also finalizes the whole-session video (session_video.mp4) and removes
    any inspiration tracks this session added to the setlist that ended up
    with no take at all."""
    log_path = session_dir / "session_log.json"
    data = json.loads(log_path.read_text())

    projects_dir = Path(config.projects_dir)
    root = vault_root(config)
    video_offset_ms = config.video_latency_compensation_ms

    project = Project.open(projects_dir / f"{data['project']}.json", root)
    instrument = data["instrument"]
    instrument_label = data.get("instrument_label", "")
    musician = data.get("musician", "")
    sample_rate = data.get("sample_rate") or 48000
    mix_start_frame = data.get("mix_start_frame", 0)

    session_flac = session_dir / "session.flac"
    session_video_raw = session_dir / "session_video_raw.mp4"
    session_mix_flac = session_dir / "session_mix.flac"
    have_video = session_video_raw.exists() and session_mix_flac.exists()

    completed = parse_session_log(data)
    filter_slot_draws = data.get("filter_slot_draws", {})

    saved = 0
    videos = 0
    # Exactly which take(s) *this* session produced, per track_index — a
    # durable snapshot written back into session_log.json below, so the
    # Sessions tab (backend.py's get_session_detail) can show only takes
    # this session actually made instead of whatever's currently on file
    # for that track (which may include takes from other sessions
    # entirely, or a since-superseded one of this session's own — see
    # get_session_detail's docstring). A list per track_index rather than
    # one entry, since a track revisited more than once in the same
    # session (e.g. redrawn/re-recorded) can produce more than one take.
    session_takes: dict[int, list[dict]] = {}
    completed_dir = ensure_dir(project.completed_takes_dir)
    if completed:
        with sf.SoundFile(str(session_flac)) as src:
            total = len(src)
            for take in completed:
                start = max(0, take.start_frame)
                end = min(take.end_frame, total)
                if end <= start:
                    continue
                if not (0 <= take.track_index < len(project.setlist.tracks)):
                    continue
                slot = project.setlist.tracks[take.track_index]
                # take.track_name (logged on this take's record_start/
                # back_to_start events) is the slot's own name for an
                # ordinary track — but for a filter slot, it's whatever
                # song actually got drawn for it this session, not the
                # slot's own label ("Random ..."). Always used for the
                # archived take's filename/watermark.
                track_name = take.track_name or slot.name
                # Take filing/numbering/filenames use the instrument's
                # LABEL, not which specific piece of gear played it — so
                # a Stratocaster take and a later Telecaster take of the
                # same song continue one "electric-guitar" take-number
                # sequence instead of splitting into separate per-gear
                # counters (they're both still "electric-guitar - take2",
                # not a fresh "Telecaster - take1"). take.instrument (the
                # full_name) stays around only for the human-readable
                # video watermark below — that's the "record which piece
                # of gear actually played this" info, and it already
                # lives in session_log.json for that purpose. Falls back
                # to the session-wide fields only for a log recorded
                # before events/labels carried their own.
                take_label = take.instrument_label or instrument_label or take.instrument or instrument
                take_full_name = take.instrument or instrument

                # For a filter slot, the take belongs to whatever song got
                # drawn this session (draw_info), not the slot's own
                # (never-populated) backing_track — needed up front so the
                # filename itself (take_filename) can name the actual
                # backing track/source, not the slot's.
                draw_info = filter_slot_draws.get(str(take.track_index)) if slot.is_inspiration_filter else None
                if slot.is_inspiration_filter:
                    take_backing_track = draw_info["backing_track"] if draw_info else ""
                    take_source = "inspiration"
                else:
                    take_backing_track = slot.backing_track
                    take_source = slot.source_label()

                take_num = next_take_number(completed_dir, track_name, take_label)
                flac_name = take_filename(track_name, take_label, take_num, take_source, take_backing_track, "flac")
                _copy_flac_segment(src, start, end, completed_dir / flac_name)

                has_video = False
                if have_video:
                    from ..video.capture import clip_session_video, format_watermark_text
                    watermark = format_watermark_text(
                        musician, take_full_name, take.start_wall_time, track_name,
                    )
                    has_video = clip_session_video(
                        session_video_raw, session_mix_flac, completed_dir / flac_name,
                        completed_dir / take_filename(
                            track_name, take_label, take_num, take_source, take_backing_track, "mp4",
                        ),
                        mix_start_s=(start - mix_start_frame) / sample_rate,
                        duration_s=(end - start) / sample_rate,
                        watermark_text=watermark, video_offset_ms=video_offset_ms,
                    )
                    videos += has_video

                take_info = TakeInfo(
                    instrument=take_label, take_number=take_num, filename=flac_name, has_video=has_video,
                    input_label=take.input_label or input_label,
                )
                if slot.is_inspiration_filter:
                    # Recorded into the shared vault-wide index (vault.py),
                    # not the slot's own top-level preferred_takes — the
                    # slot itself must stay "always needs a take" so it
                    # keeps getting redrawn. draw_info (this session's
                    # actual draw, from backend.py's _save_session_log) is
                    # what lets a later session — this project or any
                    # other — recognize and prefer the same song (see
                    # backend.py's _resolve_filter_slot).
                    if draw_info:
                        record_inspiration_take(
                            root, draw_info["inspiration_track_id"], draw_info["name"],
                            draw_info["backing_track"], draw_info.get("duration_seconds", 0.0),
                            take_label, take_info,
                        )
                else:
                    slot.set_preferred_take(take_label, take_info)
                    if slot.inspiration_track_id:
                        # A regular (non-filter) inspiration-sourced track:
                        # mirror the take into the shared index too, so any
                        # *other* project referencing this same song can
                        # find it — same reuse mechanism a filter slot's
                        # draw gets, just recorded alongside the ordinary
                        # setlist entry rather than instead of it.
                        record_inspiration_take(
                            root, slot.inspiration_track_id, slot.name, slot.backing_track,
                            slot.duration_seconds, take_label, take_info,
                        )
                session_takes.setdefault(take.track_index, []).append(
                    {"instrument": take_label, **asdict(take_info)}
                )
                saved += 1

    project.save_setlist()

    if session_takes:
        # Snapshot written back into session_log.json itself — see
        # session_takes' own docstring above for why get_session_detail
        # needs this rather than reading current preferred_takes state.
        data["takes"] = {str(k): v for k, v in session_takes.items()}
        atomic_write_text(log_path, json.dumps(data, indent=2))

    # The whole-session archive video, watermarked once for the session.
    session_video_ok = False
    if have_video:
        from ..video.capture import format_watermark_text, mux_video_audio
        first_track = next((e.get("track_name", "") for e in data.get("events", [])
                            if e.get("event_type") == "record_start"), "")
        start_wall = next((e.get("wall_time", "") for e in data.get("events", [])
                           if e.get("event_type") == "session_start"), "")
        watermark = format_watermark_text(musician, instrument, start_wall, first_track)
        session_video_ok = mux_video_audio(
            session_video_raw, session_mix_flac, session_flac,
            session_dir / "session_video.mp4",
            watermark_text=watermark, video_offset_ms=video_offset_ms,
        )
        if session_video_ok:
            session_video_raw.unlink(missing_ok=True)
            session_mix_flac.unlink(missing_ok=True)
            # The instrument-only track muxed into session_video.mp4 above is a
            # lossless, bit-identical copy of session.flac's samples, so once
            # the mux succeeds this standalone file is pure redundancy. Kept
            # only for audio-only sessions (no camera), where it's the sole
            # recording.
            session_flac.unlink(missing_ok=True)

    take_word = "take" if saved == 1 else "takes"
    summary = f"Session processed: {saved} completed {take_word} saved"
    if videos:
        summary += f" ({videos} with video)"
    if have_video and not session_video_ok:
        summary += "; session video mux failed, raw files kept"
    return summary + "."
