Name:           yuyo
Version:        1.0.0
Release:        2%{?dist}
Summary:        Lightweight GTK3 audio mixer for Linux MATE
License:        GPL-3.0-only
URL:            https://github.com/solo2101/Yuyo
Source0:        yuyo.py
BuildArch:      noarch

Requires:       python3
Requires:       python3-gobject
Requires:       gtk3
Requires:       pulseaudio-utils

%description
Yuyo is a lightweight GTK3 audio mixer for Linux MATE,
supporting PipeWire and PulseAudio.

%prep

%build

%install
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_datadir}/applications
mkdir -p %{buildroot}%{_sysconfdir}/xdg/autostart

install -m 755 %{SOURCE0} %{buildroot}%{_bindir}/yuyo

cat > %{buildroot}%{_datadir}/applications/yuyo.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Yuyo
Comment=GTK3 Audio Mixer
Exec=yuyo
Icon=multimedia-volume-control
Terminal=false
Categories=AudioVideo;Audio;Mixer;
DESKTOP

cat > %{buildroot}%{_sysconfdir}/xdg/autostart/yuyo.desktop <<'AUTOSTART'
[Desktop Entry]
Type=Application
Name=Yuyo
Comment=Start Yuyo audio mixer at login
Exec=yuyo
Icon=multimedia-volume-control
Terminal=false
OnlyShowIn=MATE;
X-GNOME-Autostart-enabled=true
AUTOSTART

%files
%{_bindir}/yuyo
%{_datadir}/applications/yuyo.desktop
%config(noreplace) %{_sysconfdir}/xdg/autostart/yuyo.desktop

%changelog
* Fri Nov 13 2026 Yuyo Project - 1.0.0-2
- Start Yuyo automatically at MATE login
* Fri Nov 13 2026 Yuyo Project - 1.0.0-1
- Initial release
