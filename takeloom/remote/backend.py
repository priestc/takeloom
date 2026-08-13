"""RemoteBackend: adapts takeloom.backend.Backend over a RemoteClient, so UI
tabs can't tell whether they're talking to local hardware or a remote
takeloom instance."""

from __future__ import annotations

import base64
import tempfile
import threading
from pathlib import Path
from typing import Callable

from ..backend import (
    Backend,
    BackendError,
    EventCallback,
    FrameCallback,
    PreviewSubscription,
    StartRecordingRequest,
)
from ..config import StudioConfig
from ..utils import ensure_dir
from .client import RemoteClient

# Recording/inspiration calls touch remote disk, network, and hardware —
# give them more room than simple config/device lookups.
LONG_TIMEOUT = 20.0

# yt-dlp downloads can take a while on a slow connection or a long video.
DOWNLOAD_TIMEOUT = 300.0


class RemoteBackend(Backend):
    def __init__(self, client: RemoteClient) -> None:
        self._client = client
        self._event_callbacks: list[EventCallback] = []
        self._preview_lock = threading.Lock()
        self._preview_callbacks: list[FrameCallback] = []
        self._preview_subscribed = False
        self._video_check_result_buffer = bytearray()
        self._take_file_buffer = bytearray()
        client._on_event = self._on_raw_event

    def is_remote(self) -> bool:
        return True

    def hostname(self) -> str:
        return self._client.hostname

    def close(self) -> None:
        self._client.close()

    # --- config ---

    def get_config(self) -> StudioConfig:
        result = self._client.call("get_config", {})
        return StudioConfig.from_dict(result["config"])

    def save_config(self, config: StudioConfig) -> None:
        self._client.call("save_config", {"config": config.to_dict()})

    # --- devices ---

    def list_audio_devices(self) -> list[dict]:
        return self._client.call("list_audio_devices", {})["devices"]

    def list_cameras(self) -> list[tuple[str, str]]:
        return [tuple(c) for c in self._client.call("list_cameras", {})["cameras"]]

    def refresh_devices(self) -> None:
        self._client.call("refresh_devices", {})

    # --- projects / setlists ---

    def list_projects(self) -> list[str]:
        return self._client.call("list_projects", {})["projects"]

    def get_setlist(self, project_name: str) -> dict:
        return self._client.call("get_setlist", {"project_name": project_name})["setlist"]

    def save_setlist(self, project_name: str, setlist_data: dict) -> None:
        self._client.call("save_setlist", {"project_name": project_name, "setlist": setlist_data})

    def create_project(self, name: str) -> str:
        return self._client.call("create_project", {"name": name}, timeout=LONG_TIMEOUT)["name"]

    def add_local_backing_track(
        self, project_name: str, source_path: str, track_name: str | None = None,
    ) -> dict:
        raise BackendError(
            "Adding local files as backing tracks isn't available over Remote connections "
            "yet — paste a YouTube URL instead."
        )

    def add_youtube_backing_track(
        self, project_name: str, url: str, on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        if on_progress:
            on_progress(None, "Downloading on the remote studio (live progress isn't available over Remote yet)...")
        return self._client.call(
            "add_youtube_backing_track", {"project_name": project_name, "url": url}, timeout=DOWNLOAD_TIMEOUT,
        )

    def add_inspiration_filter_slot(self, project_name: str, label: str, filter_criteria: dict) -> dict:
        return self._client.call(
            "add_inspiration_filter_slot",
            {"project_name": project_name, "label": label, "filter_criteria": filter_criteria},
        )

    def get_filter_slot_previews(self, project_name: str) -> list[dict | None]:
        return self._client.call(
            "get_filter_slot_previews", {"project_name": project_name}, timeout=LONG_TIMEOUT,
        )["previews"]

    # --- sessions (browse/correct past recordings) ---

    def list_sessions(self) -> list[dict]:
        return self._client.call("list_sessions", {})["sessions"]

    def get_session_detail(self, session_dir: str) -> dict:
        return self._client.call("get_session_detail", {"session_dir": session_dir})

    def correct_session_instrument(self, session_dir: str, new_instrument: str) -> None:
        self._client.call(
            "correct_session_instrument", {"session_dir": session_dir, "new_instrument": new_instrument},
        )

    def reassign_take(self, session_dir: str, track_name: str, old_instrument: str, new_instrument: str) -> None:
        self._client.call(
            "reassign_take",
            {
                "session_dir": session_dir, "track_name": track_name,
                "old_instrument": old_instrument, "new_instrument": new_instrument,
            },
        )

    def analyze_take(self, session_dir: str, track_name: str, instrument_name: str) -> dict:
        return self._client.call(
            "analyze_take",
            {"session_dir": session_dir, "track_name": track_name, "instrument_name": instrument_name},
        )

    def list_completed_takes(self) -> list[dict]:
        return self._client.call("list_completed_takes", {})["takes"]

    def ensure_take_local(self, project_name: str, filename: str) -> str:
        raise BackendError(
            "ensure_take_local downloads to whichever machine runs it — over Remote that's the "
            "studio's own disk, not this one. Use play_take instead."
        )

    def play_take(self, project_name: str, filename: str) -> None:
        # Server resolves/downloads the file on its own end (see backend.
        # py's ensure_take_local) and streams it back in chunks as
        # "take_file" events on this same connection (see _on_raw_event)
        # rather than one giant RPC response — same reasoning as
        # RemoteServer.broadcast_file's own docstring. The "fetch_take_
        # file" op's server-side handler (remote/server.py) sends every
        # chunk *before* its RPC response, and this connection's single
        # reader thread processes lines strictly in order, so by the time
        # this call() returns, every chunk has already been received and
        # appended to self._take_file_buffer by _on_raw_event — no extra
        # wait/Event needed here.
        self._take_file_buffer = bytearray()
        self._client.call(
            "fetch_take_file", {"project_name": project_name, "filename": filename}, timeout=DOWNLOAD_TIMEOUT,
        )
        data = bytes(self._take_file_buffer)
        if not data:
            raise BackendError(f"No data received for '{filename}'.")
        work_dir = ensure_dir(Path(tempfile.gettempdir()) / "takeloom_remote_takes")
        local_path = work_dir / filename
        local_path.write_bytes(data)
        from ..video.capture import open_in_default_player
        open_in_default_player(local_path)

    # --- inspiration ---

    def search_inspiration_artists(self, partial: str) -> list[str]:
        try:
            return self._client.call("search_inspiration_artists", {"partial": partial})["suggestions"]
        except BackendError:
            return []  # autocomplete fires on every keystroke — a connection hiccup shouldn't surface as an error

    def search_inspiration_by_filter(self, filter_criteria: dict) -> list[dict]:
        return self._client.call(
            "search_inspiration_by_filter", {"filter_criteria": filter_criteria}, timeout=LONG_TIMEOUT,
        )["tracks"]

    # --- recording ---

    def start_recording(self, req: StartRecordingRequest) -> None:
        self._client.call(
            "start_recording",
            {
                "project_name": req.project_name,
                "instrument_name": req.instrument_name,
                "track_index": req.track_index,
            },
            timeout=LONG_TIMEOUT,
        )

    def unpause_recording(self) -> None:
        self._client.call("unpause_recording", {}, timeout=LONG_TIMEOUT)

    def stop_recording(self) -> None:
        self._client.call("stop_recording", {}, timeout=LONG_TIMEOUT)

    def restart_take(self) -> None:
        self._client.call("restart_take", {}, timeout=LONG_TIMEOUT)

    def next_track(self) -> None:
        # Can involve a backing-track download on the server side.
        self._client.call("next_track", {}, timeout=LONG_TIMEOUT)

    def redraw_current_track(self) -> None:
        # Can involve an inspiration-server query + download on the server side.
        self._client.call("redraw_current_track", {}, timeout=LONG_TIMEOUT)

    def is_recording(self) -> bool:
        return self._client.call("is_recording", {})["recording"]

    def adjust_backing_volume(self, delta: int) -> None:
        self._client.call("adjust_backing_volume", {"delta": delta})

    def adjust_takes_volume(self, delta: int) -> None:
        self._client.call("adjust_takes_volume", {"delta": delta})

    def adjust_instrument_volume(self, delta: int) -> None:
        self._client.call("adjust_instrument_volume", {"delta": delta})

    # --- audio filters ---

    def get_compressor_settings(self) -> dict:
        return self._client.call("get_compressor_settings", {})["settings"]

    def set_compressor_settings(self, settings: dict) -> None:
        self._client.call("set_compressor_settings", {"settings": settings})

    # --- live monitoring mode ---

    def get_monitoring_mode(self) -> str:
        return self._client.call("get_monitoring_mode", {})["mode"]

    def set_monitoring_mode(self, mode: str) -> None:
        self._client.call("set_monitoring_mode", {"mode": mode})

    def restart_monitoring(self) -> bool:
        return self._client.call("restart_monitoring", {})["monitoring"]

    def on_event(self, callback: EventCallback) -> None:
        self._event_callbacks.append(callback)

    def off_event(self, callback: EventCallback) -> None:
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    # --- camera preview ---

    def open_camera_preview(self, on_frame: FrameCallback) -> PreviewSubscription:
        with self._preview_lock:
            self._preview_callbacks.append(on_frame)
            if not self._preview_subscribed:
                self._preview_subscribed = True
                try:
                    self._client.call("subscribe_preview", {})
                except BackendError:
                    self._preview_subscribed = False
        return _RemotePreviewSubscription(self, on_frame)

    # --- camera latency test (not supported over Remote; see LatencyFrame) ---

    def start_latency_test(self, instrument_name: str, camera_device: str, play_metronome: bool = True) -> None:
        raise BackendError("Camera latency measurement isn't available over Remote connections yet.")

    def stop_latency_test(self) -> None:
        raise BackendError("Camera latency measurement isn't available over Remote connections yet.")

    # --- instrument train / detect-all (not supported over Remote; see StudioSetupFrame) ---

    def start_instrument_train(self, instrument_name: str) -> None:
        raise BackendError("Instrument detect/train isn't available over Remote connections yet.")

    def stop_instrument_test(self) -> None:
        raise BackendError("Instrument detect/train isn't available over Remote connections yet.")

    def start_detect_all(self) -> None:
        raise BackendError("Instrument detect/train isn't available over Remote connections yet.")

    def stop_detect_all(self) -> None:
        raise BackendError("Instrument detect/train isn't available over Remote connections yet.")

    # --- auto-detect instrument (remote-capable — see Backend's docstring;
    # unlike everything above, the Record tab actually needs this to work
    # over Remote, since that's the normal way a session gets recorded) ---

    def start_auto_detect_instrument(self) -> None:
        self._client.call("start_auto_detect_instrument", {}, timeout=LONG_TIMEOUT)

    def stop_auto_detect_instrument(self) -> None:
        self._client.call("stop_auto_detect_instrument", {})

    # --- video check (not supported over Remote; see RecordFrame) ---

    def start_video_check(self, req: StartRecordingRequest) -> None:
        raise BackendError("Video check isn't available over Remote connections yet.")

    def stop_video_check(self) -> None:
        raise BackendError("Video check isn't available over Remote connections yet.")

    # --- session lifecycle ---
    # A remote client never needs these directly: start_recording() on the
    # server opens the session itself, and stop_recording() closes it —
    # both already exposed above. Explicit open/close stays local-only.

    def begin_session(self, project_name: str, instrument_name: str) -> None:
        raise BackendError("Explicit session control isn't available over Remote — just start recording.")

    def end_session(self) -> None:
        raise BackendError("Explicit session control isn't available over Remote — just stop recording.")

    def _unsubscribe_preview(self, on_frame: FrameCallback) -> None:
        with self._preview_lock:
            if on_frame in self._preview_callbacks:
                self._preview_callbacks.remove(on_frame)
            if not self._preview_callbacks and self._preview_subscribed:
                self._preview_subscribed = False
                try:
                    self._client.call("unsubscribe_preview", {})
                except BackendError:
                    pass

    # --- event dispatch from the RemoteClient's reader thread ---

    def _on_raw_event(self, event: str, data: dict) -> None:
        if event == "preview_frame":
            try:
                jpeg = base64.b64decode(data["jpeg_b64"])
            except Exception:
                return
            with self._preview_lock:
                callbacks = list(self._preview_callbacks)
            for cb in callbacks:
                try:
                    cb(jpeg)
                except Exception:
                    pass
            return

        if event == "take_file":
            # Chunked transfer behind play_take — accumulated here so
            # that by the time play_take's own call() returns (after the
            # server's "fetch_take_file" response, sent only once every
            # chunk before it has gone out), self._take_file_buffer is
            # already complete. Malformed chunks are dropped silently,
            # same as video_check_result below — play_take's own "no data
            # received" check catches the resulting empty buffer.
            try:
                seq, total = data["seq"], data["total"]
                chunk = base64.b64decode(data["data_b64"])
            except Exception:
                return
            if seq == 0:
                self._take_file_buffer = bytearray()
            self._take_file_buffer += chunk
            return

        if event == "video_check_result":
            # Chunked transfer of a video check result the server ran on its
            # own attached hardware (Video Check can't be triggered from a
            # Remote connection at all — see start_video_check() below — so
            # this is always the server's own local activity, sent here for
            # display since a headless server assumes nobody's watching it
            # directly). Only one video check can ever be active at a time
            # server-side, so chunks for a given transfer always arrive back
            # to back in order on this one connection — no per-transfer ID
            # needed, just seq==0 to (re)start the buffer.
            try:
                seq, total = data["seq"], data["total"]
                chunk = base64.b64decode(data["data_b64"])
            except Exception:
                return
            if seq == 0:
                self._video_check_result_buffer = bytearray()
            self._video_check_result_buffer += chunk
            if seq == total - 1:
                has_video = bool(data.get("has_video"))
                work_dir = ensure_dir(Path(tempfile.gettempdir()) / "takeloom_remote_video_check")
                local_path = work_dir / ("result.mp4" if has_video else "result.flac")
                local_path.write_bytes(bytes(self._video_check_result_buffer))
                self._video_check_result_buffer = bytearray()
                # Re-dispatched as an ordinary video_check_status event, now
                # naming a path that actually exists on this machine — same
                # shape RecordingDeckDriver/RecordFrame already react to for
                # a locally-triggered video check.
                for cb in list(self._event_callbacks):
                    try:
                        cb("video_check_status", {
                            "phase": "idle", "result_path": str(local_path), "has_video": has_video,
                        })
                    except Exception:
                        pass
            return

        for cb in list(self._event_callbacks):
            try:
                cb(event, data)
            except Exception:
                pass


class _RemotePreviewSubscription(PreviewSubscription):
    def __init__(self, backend: RemoteBackend, callback: FrameCallback) -> None:
        self._backend = backend
        self._callback = callback
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._backend._unsubscribe_preview(self._callback)
