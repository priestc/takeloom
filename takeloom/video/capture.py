"""VideoRecorder: continuous camera capture to disk via an ffmpeg subprocess."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from ..streaming import StreamTarget


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _input_args(device: str, framerate: int) -> list[str]:
    if sys.platform == "darwin":
        return ["-f", "avfoundation", "-framerate", str(framerate), "-i", f"{device}:none"]
    if sys.platform.startswith("linux"):
        return ["-f", "v4l2", "-framerate", str(framerate), "-i", device]
    if sys.platform.startswith("win"):
        return ["-f", "dshow", "-framerate", str(framerate), "-i", f"video={device}"]
    raise RuntimeError(f"Camera capture is not supported on platform: {sys.platform}")


def _build_capture_cmd(
    device: str, output_path: Path, framerate: int, stream_preview: bool,
    stream_target: "StreamTarget | None" = None,
) -> list[str]:
    cmd = ["ffmpeg", "-y", *_input_args(device, framerate)]
    if stream_target is not None:
        # Input 1: the live mix audio arriving over LiveAudioFeeder's FIFO,
        # as raw interleaved float32 PCM — see takeloom/streaming.py.
        cmd += [
            "-thread_queue_size", "1024",
            "-f", "f32le", "-ar", str(stream_target.sample_rate), "-ac", str(stream_target.channels),
            "-i", str(stream_target.audio_fifo),
        ]
    if not stream_preview and stream_target is None:
        return cmd + ["-pix_fmt", "yuv420p", str(output_path)]

    # Split the decoded camera feed into up to three branches: "rec" (always,
    # encoded to the local output file exactly as before), "prev" (scaled
    # down to ~10fps MJPEG on stdout, for the live preview panel while this
    # process holds the camera device exclusively — see on_preview_frame),
    # and "streamv" (scaled down and encoded live to the RTMP target). RTMP
    # streaming can't be a separate ffmpeg process sharing the same camera
    # device, hence tacking it onto this one instead — see streaming.py.
    branches = ["rec"]
    if stream_preview:
        branches.append("prev")
    if stream_target is not None:
        branches.append("streamv")
    filter_parts = [f"[0:v]split={len(branches)}" + "".join(f"[{b}]" for b in branches)]
    if stream_preview:
        filter_parts.append("[prev]fps=10,scale=320:-2[prevout]")
    if stream_target is not None:
        # width <= 0 means "stream at the source resolution" — `null` is
        # ffmpeg's video passthrough filter (there's no scaling filter for
        # "don't scale"), just relabeling the branch for the -map below.
        stream_filter = f"scale={stream_target.width}:-2" if stream_target.width > 0 else "null"
        filter_parts.append(f"[streamv]{stream_filter}[streamout]")

    cmd += ["-filter_complex", ";".join(filter_parts)]
    cmd += ["-map", "[rec]", "-pix_fmt", "yuv420p", str(output_path)]
    if stream_preview:
        cmd += ["-map", "[prevout]", "-f", "mjpeg", "-q:v", "5", "pipe:1"]
    if stream_target is not None:
        bitrate = f"{stream_target.bitrate_kbps}k"
        cmd += [
            "-map", "[streamout]", "-map", "1:a",
            # No "-tune zerolatency": it forces x264 into slice-based frame
            # threading, and combined with the tight -maxrate/-bufsize below
            # (needed to hit a live-streaming bitrate target at all), a
            # high-core-count machine + a busy/detailed frame can starve
            # individual slices of bits, corrupting the frame in visible
            # horizontal bands — reproduced locally: identical corruption
            # with the tune on, gone with it off. A live stream's few extra
            # seconds of encoder latency are irrelevant here anyway (YouTube
            # itself buffers several seconds on ingest before playback).
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{stream_target.bitrate_kbps * 2}k",
            "-g", str(framerate * 2), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
            "-f", "flv", stream_target.rtmp_url,
        ]
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

    If stream_target is given, ffmpeg also encodes a third branch of the
    same camera feed live to an RTMP target (see takeloom/streaming.py) —
    for the same camera-exclusivity reason, this rides along on this
    process rather than a separate one.
    """

    def __init__(
        self,
        device: str,
        output_path: Path,
        framerate: int = 30,
        on_preview_frame: Callable[[bytes], None] | None = None,
        stream_target: "StreamTarget | None" = None,
    ) -> None:
        self.device = device
        self.output_path = output_path
        self.framerate = framerate
        self._on_preview_frame = on_preview_frame
        self.stream_target = stream_target
        self._proc: subprocess.Popen | None = None
        self._preview_thread: threading.Thread | None = None

    def start(self) -> bool:
        """Launch the ffmpeg capture process. Returns False if ffmpeg is unavailable."""
        if not ffmpeg_available():
            return False
        stream_preview = self._on_preview_frame is not None
        cmd = _build_capture_cmd(self.device, self.output_path, self.framerate, stream_preview, self.stream_target)
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if stream_preview else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if stream_preview:
            self._preview_thread = threading.Thread(
                target=self._read_preview_frames, args=(self._proc,), daemon=True,
            )
            self._preview_thread.start()
        return True

    def _read_preview_frames(self, proc: subprocess.Popen) -> None:
        """Split ffmpeg's raw MJPEG stdout stream into individual JPEG frames
        (consecutive images, each delimited by its own SOI/EOI markers) and
        hand each to on_preview_frame."""
        buf = b""
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    return
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start == -1:
                        buf = b""
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        if start > 0:
                            buf = buf[start:]
                        break
                    frame, buf = buf[start:end + 2], buf[end + 2:]
                    try:
                        self._on_preview_frame(frame)
                    except Exception:
                        pass
        except (OSError, ValueError):
            pass

    def stop(self, timeout: float = 10.0) -> None:
        """Signal ffmpeg to finish and finalize the output file."""
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.wait(timeout=timeout)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self._preview_thread is not None:
            self._preview_thread.join(timeout=2.0)
            self._preview_thread = None

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
            "ffprobe", "-v", "error", "-select_streams", "v:0",
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
) -> bool:
    """Combine a silent video file with two audio tracks: a compressed mix
    (backing track + instrument, for easy playback/sharing) and a lossless
    instrument-only track (for anyone who wants to remix or isolate it).

    If watermark_text is given, burns it into a small translucent bar along
    the bottom of the video. Rendered as a PNG overlay via Pillow rather than
    ffmpeg's drawtext filter, since drawtext requires ffmpeg to be built with
    libfreetype, which many default/minimal ffmpeg builds omit.

    video_offset_ms comes from the Latency tab's camera test: positive means
    the video needs to be delayed relative to the audio, negative means the
    audio needs to be delayed relative to the video. Implemented as a
    positive ffmpeg -itsoffset on whichever side needs delaying, since
    negative -itsoffset isn't reliably supported across demuxers.
    """
    if not ffmpeg_available():
        return False

    watermark_png: Path | None = None
    if watermark_text:
        try:
            width, height = probe_video_size(video_path)
            watermark_png = output_path.with_name(output_path.stem + "_watermark.png")
            render_watermark_image(watermark_text, width, height, watermark_png)
        except Exception:
            watermark_png = None

    video_delay_s = max(0.0, video_offset_ms / 1000.0)
    audio_delay_s = max(0.0, -video_offset_ms / 1000.0)

    cmd = ["ffmpeg", "-y"]
    if video_delay_s:
        cmd += ["-itsoffset", f"{video_delay_s:.6f}"]
    cmd += ["-i", str(video_path)]
    if audio_delay_s:
        cmd += ["-itsoffset", f"{audio_delay_s:.6f}"]
    cmd += ["-i", str(mix_audio_path)]
    if audio_delay_s:
        cmd += ["-itsoffset", f"{audio_delay_s:.6f}"]
    cmd += ["-i", str(instrument_audio_path)]
    if watermark_png is not None:
        cmd += [
            "-i", str(watermark_png),
            "-filter_complex", "[0:v][3:v]overlay=0:main_h-overlay_h[outv]",
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
    if watermark_png is not None:
        watermark_png.unlink(missing_ok=True)
    return result.returncode == 0


def clip_session_video(
    raw_video: Path,
    mix_flac: Path,
    instrument_flac: Path,
    output_path: Path,
    mix_start_s: float,
    duration_s: float,
    watermark_text: str | None = None,
    video_offset_ms: float = 0.0,
) -> bool:
    """Cut one take's segment out of a continuous session recording: video
    from the session's raw camera file, "Mix" audio from the session mix
    flac (both seeked to the take's position on the mix/video timeline),
    and the lossless instrument-only track from the take's own already-
    spliced flac. Same two-audio-track output layout as mux_video_audio.

    `mix_start_s` is where the take begins on the mix/video timeline;
    video_offset_ms (the Latency tab's camera measurement) shifts the video
    seek the same way mux_video_audio's -itsoffset shifts a whole-file mux.
    The video is re-encoded — input seeking with a re-encode is frame-
    accurate, where a stream copy could only start at a keyframe."""
    if not ffmpeg_available() or duration_s <= 0:
        return False

    watermark_png: Path | None = None
    if watermark_text:
        try:
            width, height = probe_video_size(raw_video)
            watermark_png = output_path.with_name(output_path.stem + "_watermark.png")
            render_watermark_image(watermark_text, width, height, watermark_png)
        except Exception:
            watermark_png = None

    video_start_s = max(0.0, mix_start_s - video_offset_ms / 1000.0)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{video_start_s:.6f}", "-i", str(raw_video),
        "-ss", f"{max(0.0, mix_start_s):.6f}", "-i", str(mix_flac),
        "-i", str(instrument_flac),
    ]
    if watermark_png is not None:
        cmd += [
            "-i", str(watermark_png),
            "-filter_complex", "[0:v][3:v]overlay=0:main_h-overlay_h[outv]",
            "-map", "[outv]",
        ]
    else:
        cmd += ["-map", "0:v"]
    cmd += [
        "-map", "1:a", "-map", "2:a",
        "-t", f"{duration_s:.6f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a:0", "aac",
        "-c:a:1", "flac",
        "-metadata:s:a:0", "title=Mix",
        "-metadata:s:a:1", "title=Instrument Only",
        "-disposition:a:0", "0",
        "-disposition:a:1", "default",
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if watermark_png is not None:
        watermark_png.unlink(missing_ok=True)
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
