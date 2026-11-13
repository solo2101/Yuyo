# Yuyo

A lightweight audio mixer for Linux MATE.

Yuyo is a GTK3 audio mixer for PipeWire and PulseAudio with dedicated **Output**, **Input**, and **Hardware** tabs.

## Features

- Vertical volume sliders, mute controls, and live audio level meters
- Playback and recording device selection
- Hardware card status and available audio profiles
- Detects audio device changes
- System tray icon with volume controls
- Theme-aware icons

## Requirements

- Python 3
- GTK 3 and PyGObject (`python3-gobject`)
- PulseAudio utilities (`pactl` and `parec`)
- PipeWire with PulseAudio compatibility, or PulseAudio
- Linux desktop with a system tray, designed for MATE

## Run on Fedora

```bash
sudo dnf install python3-gobject gtk3 pulseaudio-utils
python3 yuyo.py
```

To launch directly:

```bash
chmod +x yuyo.py
./yuyo.py
```

When you close the window, Yuyo stays available from the system tray. Use **Quit** from its tray menu to exit.

## Version

1.0.0. Initial public release, based on the v33.6 development build.

## License

GNU General Public License v3.0 only. See [LICENSE](LICENSE).
