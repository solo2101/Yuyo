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

## Build an RPM on Fedora

Install the RPM build tools:

```bash
sudo dnf install rpm-build rpmdevtools
rpmdev-setuptree
```

Download the source and RPM spec:

```bash
curl -fL https://raw.githubusercontent.com/solo2101/Yuyo/main/yuyo.py -o ~/rpmbuild/SOURCES/yuyo.py
curl -fL https://raw.githubusercontent.com/solo2101/Yuyo/main/packaging/yuyo.spec -o ~/rpmbuild/SPECS/yuyo.spec
```

Build and install:

```bash
rpmbuild -bb ~/rpmbuild/SPECS/yuyo.spec
sudo dnf install ~/rpmbuild/RPMS/noarch/yuyo-1.0.0-1.*.noarch.rpm
```

The Fedora release suffix in the RPM filename depends on the build system.

The RPM installs a system-wide MATE autostart entry. Yuyo launches automatically at the next MATE login. To disable autostart for your account, use MATE Startup Applications.

For an existing RPM installation, install the new 1.0.0-2 package to upgrade.

## Version

1.0.0. Initial public release, based on the v33.6 development build.

## License

GNU General Public License v3.0 only. See [LICENSE](LICENSE).
