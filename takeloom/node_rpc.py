"""The operations the vault's web UI can run on a recording node.

The browser talks to the vault (tank2); the vault passes each operation to
the node over the node's connection to it (phone_camera/vault_hub.py's
"rpc" messages; the vault's camera_hub.py relays them), and the node runs
it here against its own LocalBackend. This op table is what remote mode
(a takeloom UI controlling another machine's takeloom server) used to
expose; remote mode is gone, every UI runs on its own machine's hardware,
and controlling a node from elsewhere is the vault's web UI's job.
"""

from __future__ import annotations

from .backend import Backend, BackendError, StartRecordingRequest
from .config import StudioConfig


def dispatch(backend: Backend, op: str, args: dict) -> dict:
    """Run one RPC op against a local backend, returning its JSON-able result."""
    if op == "hostname":
        return {"hostname": backend.hostname()}
    if op == "get_config":
        return {"config": backend.get_config().to_dict()}
    if op == "save_config":
        backend.save_config(StudioConfig.from_dict(args["config"]))
        return {}
    if op == "begin_vault_pairing":
        return backend.begin_vault_pairing(args["address"])
    if op == "poll_vault_pairing":
        return backend.poll_vault_pairing(args["url"], args["request_id"])
    if op == "check_vault_server":
        return backend.check_vault_server()
    if op == "list_audio_devices":
        return {"devices": backend.list_audio_devices()}
    if op == "list_cameras":
        return {"cameras": [list(c) for c in backend.list_cameras()]}
    if op == "session_cameras":
        return {"cameras": [list(c) for c in backend.session_cameras()]}
    if op == "camera_inventory":
        return {"cameras": backend.camera_inventory()}
    if op == "camera_versions":
        return {"versions": backend.camera_versions()}
    if op == "get_program_camera":
        return {"camera": backend.get_program_camera()}
    if op == "set_program_camera":
        backend.set_program_camera(args["camera"])
        return {}
    if op == "camera_batteries":
        return {"batteries": backend.camera_batteries()}
    if op == "get_camera_framing":
        return backend.get_camera_framing(args["camera"])
    if op == "set_camera_framing":
        return backend.set_camera_framing(args["camera"], float(args["zoom"]), float(args["x"]), float(args["y"]))
    if op == "list_midi_devices":
        return {"devices": backend.list_midi_devices()}
    if op == "refresh_devices":
        backend.refresh_devices()
        return {}
    if op == "list_projects":
        return {"projects": backend.list_projects()}
    if op == "get_setlist":
        return {"setlist": backend.get_setlist(args["project_name"])}
    if op == "save_setlist":
        backend.save_setlist(args["project_name"], args["setlist"])
        return {}
    if op == "create_project":
        return {"name": backend.create_project(args["name"])}
    if op == "add_local_backing_track":
        return backend.add_local_backing_track(args["project_name"], args["source_path"], args.get("track_name"))
    if op == "add_youtube_backing_track":
        return backend.add_youtube_backing_track(args["project_name"], args["url"])
    if op == "add_inspiration_backing_track":
        return backend.add_inspiration_backing_track(args["project_name"], args["artist"], args["title"])
    if op == "add_song_set_slot":
        return backend.add_song_set_slot(args["project_name"], args["label"], args["songs"])
    if op == "find_inspiration_track":
        return {"track": backend.find_inspiration_track(args["artist"], args["title"])}
    if op == "get_filter_slot_previews":
        return {"previews": backend.get_filter_slot_previews(args["project_name"])}
    if op == "list_sessions":
        return {"sessions": backend.list_sessions()}
    if op == "get_session_detail":
        return backend.get_session_detail(args["session_dir"])
    if op == "process_pending_session":
        return {"summary": backend.process_pending_session(args["session_dir"])}
    if op == "delete_session":
        return backend.delete_session(args["session_dir"])
    if op == "correct_session_instrument":
        backend.correct_session_instrument(args["session_dir"], args["new_instrument"])
        return {}
    if op == "reassign_take":
        backend.reassign_take(
            args["session_dir"], args["track_name"], args["old_instrument"], args["new_instrument"],
        )
        return {}
    if op == "analyze_take":
        return backend.analyze_take(args["session_dir"], args["track_name"], args["instrument_name"])
    if op == "list_completed_takes":
        return {"takes": backend.list_completed_takes()}
    if op == "set_preferred_take":
        backend.set_preferred_take(args["take_filename"], args["instrument"], args["new_filename"])
        return {}
    if op == "get_song_mix":
        return {"mix": backend.get_song_mix(args["track_name"])}
    if op == "save_song_mix":
        return {"mix": backend.save_song_mix(args["track_name"], args["volumes"], args["muted"])}
    if op == "edit_backing_track":
        return backend.edit_backing_track(
            args["take_filename"], args["trim_start_seconds"], args["trim_end_seconds"],
        )
    if op == "start_auto_detect_instrument":
        backend.start_auto_detect_instrument()
        return {}
    if op == "stop_auto_detect_instrument":
        backend.stop_auto_detect_instrument()
        return {}
    if op == "search_inspiration_artists":
        return {"suggestions": backend.search_inspiration_artists(args["partial"])}
    if op == "search_inspiration_titles":
        return {"suggestions": backend.search_inspiration_titles(args["partial"], args.get("artist", ""))}
    if op == "add_inspiration_track_by_id":
        return backend.add_inspiration_track_by_id(args["project_name"], args["track_info"])
    if op == "search_inspiration_by_filter":
        return {"tracks": backend.search_inspiration_by_filter(
            args["filter_criteria"], all_matches=bool(args.get("all_matches")),
        )}
    if op == "start_recording":
        req = StartRecordingRequest(
            project_name=args["project_name"],
            instrument_name=args["instrument_name"],
            track_index=args["track_index"],
        )
        backend.start_recording(req)
        return {}
    if op == "unpause_recording":
        backend.unpause_recording()
        return {}
    if op == "stop_recording":
        backend.stop_recording()
        return {}
    if op == "restart_take":
        backend.restart_take()
        return {}
    if op == "next_track":
        backend.next_track()
        return {}
    if op == "redraw_current_track":
        backend.redraw_current_track()
        return {}
    if op == "is_recording":
        return {"recording": backend.is_recording()}
    if op == "adjust_backing_volume":
        backend.adjust_backing_volume(args["delta"])
        return {}
    if op == "adjust_takes_volume":
        backend.adjust_takes_volume(args["delta"])
        return {}
    if op == "adjust_instrument_volume":
        backend.adjust_instrument_volume(args["delta"])
        return {}
    if op == "set_compressor_settings":
        backend.set_compressor_settings(args["label"], args["settings"])
        return {}
    if op == "set_synth_voice":
        backend.set_synth_voice(args["instrument_name"], args["voice"])
        return {}
    if op == "get_voice_switch":
        return {"target": backend.get_voice_switch()}
    if op == "cycle_synth_voice":
        return {"target": backend.cycle_synth_voice()}
    if op == "benchmark_audio_modifiers":
        return backend.benchmark_audio_modifiers()
    if op == "get_monitoring_mode":
        return {"mode": backend.get_monitoring_mode()}
    if op == "set_monitoring_mode":
        backend.set_monitoring_mode(args["mode"])
        return {}
    if op == "restart_monitoring":
        return {"monitoring": backend.restart_monitoring()}
    raise BackendError(f"Unknown operation: {op}")
