# Prerequisites

Takeloom requires a few system libraries that can't be installed via pip. Install these before running `pip install`.

## Python

Python **3.9 or later** is required.

Check your version:

```bash
python3 --version
```

## System Libraries

### PortAudio

Required by `sounddevice` for real-time audio input/output (recording and playback).

Without it, Takeloom can't list or open audio devices; it says so and shows the install command (on Debian/Ubuntu, Recording Devices and Studio Setup also offer an **Install PortAudio** button, which runs `apt-get install libportaudio2` through a `pkexec` password prompt). The `.deb` package already depends on it — this only comes up for pip/pipx installs.

### libsndfile

Required by `soundfile` for reading and writing FLAC/WAV audio files.

### ffmpeg

Required for decoding MP3, M4A, AAC, and OGG backing tracks. FLAC and WAV work without it, but most users will want MP3/M4A support.

## Installation by OS

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install ffmpeg libportaudio2 portaudio19-dev libsndfile1
```

### Fedora

```bash
sudo dnf install ffmpeg portaudio portaudio-devel libsndfile
```

### Arch Linux

```bash
sudo pacman -S ffmpeg portaudio libsndfile
```

### macOS (Homebrew)

```bash
brew install ffmpeg portaudio libsndfile
```

### Windows

1. **ffmpeg** -- Download from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) or install via `winget install ffmpeg`. Make sure `ffmpeg.exe` is on your `PATH`.
2. **PortAudio / libsndfile** -- The `sounddevice` and `soundfile` pip packages bundle the necessary Windows DLLs, so no separate install is needed.

## Verifying the Install

After installing, verify each dependency:

```bash
# PortAudio — should print device list without errors
python3 -c "import sounddevice; print(sounddevice.query_devices())"

# libsndfile — should print the version string
python3 -c "import soundfile; print(soundfile.__libsndfile_version__)"

# ffmpeg — should print version info
ffmpeg -version
```

If any of these fail, double-check that the system packages above are installed.
