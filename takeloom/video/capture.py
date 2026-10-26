"""VideoRecorder: continuous camera capture to disk via an ffmpeg subprocess."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from ..ffmpeg_bin import CAMERA_FFMPEG, FFMPEG, FFPROBE
from .mjpeg import read_jpeg_frames


def ffmpeg_available() -> bool:
    return CAMERA_FFMPEG is not None


def _input_args(device: str, framerate: int) -> list[str]:
    if sys.platform == "darwin":
        return ["-f", "avfoundation", "-framerate", str(framerate), "-i", f"{device}:none"]
    if sys.platform.startswith("linux"):
        return ["-f", "v4l2", "-framerate", str(framerate), "-i", device]
    if sys.platform.startswith("win"):
        return ["-f", "dshow", "-framerate", str(framerate), "-i", f"video={device}"]
    raise RuntimeError(f"Camera capture is not supported on platform: {sys.platform}")


def _record_output_args(framerate: int) -> list[str]:
    """Encoding/muxing options for the recorded file itself.

    Fragmented MP4: a regular MP4's index (the moov atom) is only written
    when ffmpeg exits cleanly, so a capture that gets killed leaves an
    unplayable file — which is exactly what happened to every session
    ended by powering off the rig: the webcam loses power along with the
    audio interface, ffmpeg's avfoundation input blocks on the vanished
    device and never acts on stop()'s 'q', and stop() eventually has to
    kill it. With empty_moov + a fragment per keyframe the file is
    playable up to its last fragment however ffmpeg dies, and a keyframe
    every 2 s bounds what's lost to about that. Everything downstream
    (processing/splicer.py's mux_video_audio) re-muxes into a normal
    faststart MP4, so nothing else sees the difference."""
    return [
        "-pix_fmt", "yuv420p", "-g", str(framerate * 2),
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
    ]


# Preview frames' width: thumbnails in the Record tab and video-ui, and
# video-ui's large program view (see backend.py's _CameraPreviewManager).
PREVIEW_WIDTH = 640


def _build_capture_cmd(
    device: str, output_path: Path, framerate: int, stream_preview: bool,
    stream_frames_fd: int | None = None, stream_width: int = 1280,
) -> list[str]:
    cmd = [CAMERA_FFMPEG, "-y", *_input_args(device, framerate)]
    if not stream_preview and stream_frames_fd is None:
        return cmd + [*_record_output_args(framerate), str(output_path)]

    # Split the decoded camera feed into up to three branches: "rec" (always,
    # encoded to the local output file exactly as before), "prev" (scaled
    # down to ~10fps MJPEG on stdout, for the live preview panel while this
    # process holds the camera device exclusively — see on_preview_frame),
    # and "stv" (stream-quality MJPEG on another pipe, for the live stream:
    # video/stream_switcher.py streams whichever camera is the program).
    # The stream can't come from a separate ffmpeg process sharing the
    # camera device, hence tacking it onto this one.
    branches = ["rec"]
    if stream_preview:
        branches.append("prev")
    if stream_frames_fd is not None:
        branches.append("stv")
    filter_parts = [f"[0:v]split={len(branches)}" + "".join(f"[{b}]" for b in branches)]
    if stream_preview:
        filter_parts.append(f"[prev]fps=10,scale={PREVIEW_WIDTH}:-2[prevout]")
    if stream_frames_fd is not None:
        filter_parts.append(f"[stv]scale={stream_width}:-2[stvout]")

    cmd += ["-filter_complex", ";".join(filter_parts)]
    cmd += ["-map", "[rec]", *_record_output_args(framerate), str(output_path)]
    if stream_preview:
        cmd += ["-map", "[prevout]", "-f", "mjpeg", "-q:v", "5", "pipe:1"]
    if stream_frames_fd is not None:
        cmd += ["-map", "[stvout]", "-f", "mjpeg", "-q:v", "3", f"pipe:{stream_frames_fd}"]
    return cmd


class VideoRecorder:
    """Captures continuous silent video from a camera to an mp4 file.

    Runs ffmpeg as a subprocess for the lifetime of the recording. stop()
    sends 'q' on ffmpeg's stdin, which it treats as a request to finish
    up and finalize the output file cleanly (rather than leaving a
    truncated moov atom behind).

    If on_preview_frame is given, ffmpeg also tees a low-res MJPEG copy of
    the feed out to its stdout pipe while it runs — read by a background
    thread and handed to the callback frame-by-frame — so whoever's showing
    a live preview keeps seeing real camera frames even while this recorder
    holds the device exclusively (ffmpeg's avfoundation/v4l2/dshow capture
    can't share a device with anything else, e.g. a separate cv2 preview
    loop, hence needing to source the preview from here instead).

    If on_stream_frame is given, ffmpeg also tees stream-quality MJPEG
    (stream_width wide) out on a second pipe, handed to the callback frame
    by frame, for the live stream (see video/stream_switcher.py) — for the
    same camera-exclusivity reason, this rides along on this process
    rather than a separate one.
    """

    def __init__(
        self,
        device: str,
        output_path: Path,
        framerate: int = 30,
        on_preview_frame: Callable[[bytes], None] | None = None,
        on_stream_frame: Callable[[bytes], None] | None = None,
        stream_width: int = 1280,
    ) -> None:
        self.device = device
        self.output_path = output_path
        self.framerate = framerate
        self._on_preview_frame = on_preview_frame
        self._on_stream_frame = on_stream_frame
        self.stream_width = stream_width
        self._proc: subprocess.Popen | None = None
        self._preview_thread: threading.Thread | None = None

    def start(self) -> bool:
        """Launch the ffmpeg capture process. Returns False if ffmpeg is unavailable."""
        if not ffmpeg_available():
            return False
        stream_preview = self._on_preview_frame is not None
        stream_read_fd = stream_write_fd = None
        if self._on_stream_frame is not None:
            stream_read_fd, stream_write_fd = os.pipe()
        cmd = _build_capture_cmd(
            self.device, self.output_path, self.framerate, stream_preview, stream_write_fd, self.stream_width,
        )
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if stream_preview else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(stream_write_fd,) if stream_write_fd is not None else (),
        )
        if stream_preview:
            self._preview_thread = threading.Thread(
                target=read_jpeg_frames, args=(self._proc.stdout, self._on_preview_frame), daemon=True,
            )
            self._preview_thread.start()
        if stream_write_fd is not None:
            os.close(stream_write_fd)  # ffmpeg holds its own copy; EOF comes when it exits
            threading.Thread(
                target=read_jpeg_frames, args=(os.fdopen(stream_read_fd, "rb"), self._on_stream_frame),
                daemon=True,
            ).start()
        return True

    def stop(self, timeout: float = 10.0) -> bool:
        """Signal ffmpeg to finish and finalize the output file. Returns
        False if it didn't exit on its own and had to be terminated/killed
        (typically: the camera lost power mid-recording) — the fragmented
        output (see _record_output_args) is still playable up to roughly
        its last couple of seconds, but the caller should say so."""
        proc = self._proc
        self._proc = None
        clean = True
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.wait(timeout=timeout)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                clean = False
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self._preview_thread is not None:
            self._preview_thread.join(timeout=2.0)
            self._preview_thread = None
        return clean

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


def format_watermark_text(musician: str, instrument: str, when: str, backing_track: str) -> str:
    """Build the small watermark line burned into the bottom of the video."""
    parts = [p for p in (musician, instrument, when, backing_track) if p]
    return "   •   ".join(parts)


def probe_video_size(path: Path) -> tuple[int, int]:
    """Return (width, height) of a video file's first video stream."""
    result = subprocess.run(
        [
            FFPROBE, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
            str(path),
        ],
        capture_output=True, text=True,
    )
    width_str, height_str = result.stdout.strip().split("x")
    return int(width_str), int(height_str)


def _load_watermark_font(size: int):
    """Find a usable TrueType font across platforms, falling back to PIL's built-in bitmap font."""
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_watermark_image(text: str, video_width: int, video_height: int, out_path: Path) -> None:
    """Render a small translucent watermark bar, sized to the video's width."""
    from PIL import Image, ImageDraw

    bar_height = max(28, video_height // 24)
    font_size = max(13, bar_height - 12)
    font = _load_watermark_font(font_size)

    image = Image.new("RGBA", (video_width, bar_height), (0, 0, 0, 110))
    draw = ImageDraw.Draw(image)
    draw.text((14, bar_height // 2), text, font=font, fill=(255, 255, 255, 235), anchor="lm")
    image.save(out_path)


def mux_video_audio(
    video_path: Path,
    mix_audio_path: Path,
    instrument_audio_path: Path,
    output_path: Path,
    watermark_text: str | None = None,
    video_offset_ms: float = 0.0,
    watermark_schedule: list[tuple[float, str]] | None = None,
) -> bool:
    """Combine a silent video file with two audio tracks: a compressed mix
    (backing track + instrument, for easy playback/sharing) and a lossless
    instrument-only track (for anyone who wants to remix or isolate it).

    If watermark_text is given, burns it into a small translucent bar along
    the bottom of the video. Rendered as a PNG overlay via Pillow rather than
    ffmpeg's drawtext filter, since drawtext requires ffmpeg to be built with
    libfreetype, which many default/minimal ffmpeg builds omit.

    watermark_schedule instead changes the text over time: [(start_s, text),
    ...] in order, each shown from its start_s (on the output's timeline)
    until the next one's — a session video naming whichever song is
    playing at that moment. One PNG per entry, each overlaid only during its
    own interval.

    video_offset_ms comes from the Latency tab's camera test: positive means
    the video needs to be delayed relative to the audio, negative means the
    audio needs to be delayed relative to the video. Implemented as a
    positive ffmpeg -itsoffset on whichever side needs delaying, since
    negative -itsoffset isn't reliably supported across demuxers.
    """
    if not ffmpeg_available():
        return False

    if watermark_schedule is None and watermark_text:
        watermark_schedule = [(0.0, watermark_text)]
    watermark_pngs: list[Path] = []
    if watermark_schedule:
        try:
            width, height = probe_video_size(video_path)
            for i, (_start, text) in enumerate(watermark_schedule):
                png = output_path.with_name(f"{output_path.stem}_watermark{i}.png")
                render_watermark_image(text, width, height, png)
                watermark_pngs.append(png)
        except Exception:
            for png in watermark_pngs:
                png.unlink(missing_ok=True)
            watermark_pngs = []

    video_delay_s = max(0.0, video_offset_ms / 1000.0)
    audio_delay_s = max(0.0, -video_offset_ms / 1000.0)

    cmd = [FFMPEG, "-y"]
    if video_delay_s:
        cmd += ["-itsoffset", f"{video_delay_s:.6f}"]
    cmd += ["-i", str(video_path)]
    if audio_delay_s:
        cmd += ["-itsoffset", f"{audio_delay_s:.6f}"]
    cmd += ["-i", str(mix_audio_path)]
    if audio_delay_s:
        cmd += ["-itsoffset", f"{audio_delay_s:.6f}"]
    cmd += ["-i", str(instrument_audio_path)]
    if watermark_pngs:
        chain, label = [], "0:v"
        for i, png in enumerate(watermark_pngs):
            cmd += ["-i", str(png)]
            start = watermark_schedule[i][0]
            end = watermark_schedule[i + 1][0] if i + 1 < len(watermark_pngs) else None
            enable = f"gte(t,{start:.3f})" + (f"*lt(t,{end:.3f})" if end is not None else "")
            out = "outv" if i == len(watermark_pngs) - 1 else f"wm{i}"
            chain.append(f"[{label}][{3 + i}:v]overlay=0:main_h-overlay_h:enable='{enable}'[{out}]")
            label = out
        cmd += [
            "-filter_complex", ";".join(chain),
            "-map", "[outv]", "-map", "1:a", "-map", "2:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        ]
    else:
        cmd += ["-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "copy"]
    cmd += [
        "-c:a:0", "aac",
        "-c:a:1", "flac",
        "-metadata:s:a:0", "title=Mix",
        "-metadata:s:a:1", "title=Instrument Only",
        "-disposition:a:0", "0",
        "-disposition:a:1", "default",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for png in watermark_pngs:
        png.unlink(missing_ok=True)
    return result.returncode == 0


def open_in_default_player(path: Path) -> None:
    """Open a video file in the OS's default player, for reviewing a just-
    finished latency-test recording."""
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)])
    elif sys.platform.startswith("linux"):
        subprocess.run(["xdg-open", str(path)])
    elif sys.platform.startswith("win"):
        import os
        os.startfile(str(path))  # type: ignore[attr-defined]
