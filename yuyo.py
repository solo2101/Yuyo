#!/usr/bin/env python3
"""Yuyo 1.0: lightweight GTK3 audio mixer for Linux MATE and PipeWire/PulseAudio.

Requires: python3-gobject, gtk3, pulseaudio-utils
"""
import json
import math
import array
import threading
import time
import os
import configparser
from pathlib import Path
import shutil
import subprocess
import sys
import fcntl
import tempfile
from functools import lru_cache

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk

__version__ = '1.0.0'

MAX_VOLUME = 200
TOP_ICON_SIZE = 48
COLUMN_WIDTH = 166
MAX_VISIBLE_COLUMNS = 5
MIN_WINDOW_WIDTH = 560
WINDOW_SIDE_SPACE = 58
ICON_ROW_HEIGHT = 56
NAME_ROW_HEIGHT = 54
SLIDER_ROW_HEIGHT = 145
NUMBER_ROW_HEIGHT = 42
DB_ROW_HEIGHT = 26
MUTE_ROW_HEIGHT = 44
TYPE_ROW_HEIGHT = 26
SIDE_PADDING = 46



@lru_cache(maxsize=256)
def _desktop_icon_candidates(candidate_names):
    icons = []
    data_dirs = [Path.home() / ".local/share/applications"]
    data_dirs += [Path(x) / "applications" for x in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")]
    for folder in data_dirs:
        if not folder.is_dir():
            continue
        for desktop in folder.glob("*.desktop"):
            stem = desktop.stem.lower()
            if not any(c and (c in stem or stem in c) for c in candidate_names if len(c) >= 4):
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(desktop, encoding="utf-8")
                icon = parser.get("Desktop Entry", "Icon", fallback="")
                if icon:
                    icons.append(icon)
            except (OSError, configparser.Error):
                continue
    return tuple(icons)


def find_app_icon(props, name):
    """Resolve application icons from the installed desktop files and GTK theme."""
    theme = Gtk.IconTheme.get_default()
    candidates = [
        str(props.get('application.icon_name') or ''),
        str(props.get('application.id') or ''),
        str(props.get('application.process.binary') or ''),
        name.lower().replace(' ', '-'),
    ]
    low = (' '.join(candidates) + ' ' + name).lower()
    if 'firefox' in low:
        candidates += ['firefox', 'org.mozilla.firefox', 'firefox-esr']
    if 'chrome' in low:
        candidates += ['google-chrome', 'google-chrome-stable']
    if 'discord' in low:
        candidates += ['discord', 'com.discordapp.Discord']
    if 'oncehuman' in low or 'once human' in low:
        candidates += ['steam', 'applications-games']

    # Cache desktop-file lookup instead of scanning installed apps every poll.
    lookup = tuple(str(c).lower() for c in candidates if c)
    candidates = list(_desktop_icon_candidates(lookup)) + candidates

    for candidate in candidates:
        if candidate and (theme.has_icon(candidate) or Path(candidate).is_file()):
            return candidate
    return 'application-x-executable'


def themed_image(icon, size=TOP_ICON_SIZE):
    """Render a full-size icon from the current GTK icon theme."""
    theme = Gtk.IconTheme.get_default()
    if Path(icon).is_file():
        try:
            from gi.repository import GdkPixbuf
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon, size, size, True)
            return Gtk.Image.new_from_pixbuf(pixbuf)
        except (OSError, GLib.Error):
            pass
    fallback = ['audio-speakers', 'audio-speakers-symbolic', 'audio-volume-high',
                'application-x-executable', 'image-missing']
    choice = icon if theme.has_icon(icon) else next(
        (c for c in fallback if theme.has_icon(c)), 'image-missing')
    # Icon-name images follow GTK icon-theme changes automatically.
    image = Gtk.Image.new_from_icon_name(choice, Gtk.IconSize.DIALOG)
    image.set_pixel_size(size)
    return image



def pulse(*args):
    return subprocess.check_output(['pactl', *args], text=True, stderr=subprocess.PIPE)


def get_streams(kind, raw=None):
    if raw is None:
        raw = json.loads(pulse('-f', 'json', 'list', kind))
    streams = []
    for data in raw:
        props = data.get('properties') or {}
        # Our peak meters use parec recording clients. Never show them as
        # microphone applications or create channels for them.
        if kind == 'source-outputs':
            binary = str(props.get('application.process.binary', '')).rsplit('/', 1)[-1].lower()
            app_name = str(props.get('application.name', '')).lower()
            if binary == 'parec' or app_name == 'parec' or props.get('application.id') == 'yuyo-peak-meter':
                continue
        if kind == 'sinks':
            name = props.get('device.description') or data.get('description') or 'Output'
            icon = 'audio-speakers'  # Use the GTK icon theme's speaker icon for output devices
            key = 'sink'
            detail = 'Output device'
        elif kind == 'sources':
            is_monitor = (str(data.get('monitor_of_sink', '4294967295'))
                          not in ('4294967295', '-1', 'None')) or str(data.get('name', '')).endswith('.monitor')
            base_name = props.get('device.description') or data.get('description') or 'Input'
            if is_monitor:
                name = str(base_name).removesuffix(' Monitor')
                icon = 'audio-speakers'
                detail = 'Virtual source: captures audio playing through this output, not your voice'
            else:
                name = str(base_name)
                icon = 'audio-input-microphone'
                detail = 'Physical recording input: captures your microphone audio'
            key = 'source'
        else:
            name = props.get('application.name') or props.get('media.name') or 'Application'
            icon = find_app_icon(props, name)
            key = 'sink-input' if kind == 'sink-inputs' else 'source-output'
            detail = props.get('media.name') or ''
        try:
            volumes = list(data.get('volume', {}).values())
            percent = round(max(float(v.get('value_percent', '100%').strip('%')) for v in volumes))
        except (ValueError, TypeError, AttributeError):
            percent = 100
        streams.append({'key': (key, int(data['index'])), 'name': name, 'icon': icon,
                        'detail': detail, 'volume': percent, 'muted': bool(data.get('mute', False)), 'device_name': data.get('name', '')})
    return streams


def apply_volume(key, value):
    cmd = {'sink': 'set-sink-volume', 'source': 'set-source-volume',
           'sink-input': 'set-sink-input-volume', 'source-output': 'set-source-output-volume'}[key[0]]
    pulse(cmd, str(key[1]), '{}%'.format(value))


def apply_mute(key, muted):
    cmd = {'sink': 'set-sink-mute', 'source': 'set-source-mute',
           'sink-input': 'set-sink-input-mute', 'source-output': 'set-source-output-mute'}[key[0]]
    pulse(cmd, str(key[1]), '1' if muted else '0')


# Hardware state is read live from PipeWire. No local profile history.


def audio_cards():
    return json.loads(pulse('-f', 'json', 'list', 'cards'))


def hardware_devices(cards=None, sinks=None, sources=None):
    """Resolve PipeWire endpoints to their PulseAudio card, even when the
    endpoint's top-level 'card' field is absent."""
    cards = audio_cards() if cards is None else cards
    result = []
    if sinks is None:
        sinks = json.loads(pulse('-f', 'json', 'list', 'sinks'))
    if sources is None:
        sources = json.loads(pulse('-f', 'json', 'list', 'sources'))

    def card_for(endpoint):
        props = endpoint.get('properties') or {}
        raw = endpoint.get('card')
        valid = {str(c.get('index')) for c in cards}
        if raw is not None and str(raw) in valid:
            return raw
        # PipeWire exposes its device id separately from PulseAudio card index.
        # Match stable ALSA / USB properties when the direct card id is missing.
        match_keys = ('device.bus_path', 'device.serial', 'device.name',
                      'alsa.card', 'alsa.card_name', 'sysfs.path')
        for key in match_keys:
            value = props.get(key)
            if value is None or str(value) == '':
                continue
            candidates = [c for c in cards
                          if str((c.get('properties') or {}).get(key, '')) == str(value)]
            if len(candidates) == 1:
                return candidates[0]['index']
        # PipeWire node device.id is the PipeWire device object id, not
        # necessarily the PulseAudio card index. Do not assume equality.
        return None

    sink_cards = {str(x.get('index')): card_for(x) for x in sinks}
    for kind, direction, entries in (
        ('sinks', 'Playback', sinks), ('sources', 'Recording', sources)
    ):
        for data in entries:
            props = data.get('properties') or {}
            name = str(data.get('name', ''))
            monitor_sink = data.get('monitor_of_sink')
            is_monitor = kind == 'sources' and (
                name.endswith('.monitor') or
                str(monitor_sink) not in ('4294967295', '-1', 'None', ''))
            card = card_for(data)
            if is_monitor:
                direction_name = 'Playback Monitor'
                card = sink_cards.get(str(monitor_sink)) or card
                if card is None and name.endswith('.monitor'):
                    paired = next((x for x in sinks if x.get('name') == name[:-8]), None)
                    card = card_for(paired) if paired else None
            else:
                direction_name = direction
            description = (props.get('device.description') or
                           data.get('description') or name)
            result.append({
                'id': name, 'direction': direction_name,
                'name': description, 'card': card, 'index': data.get('index'),
                'is_monitor': is_monitor
            })
    return result


class LevelMeter(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.level = 0.0
        self.set_size_request(12, 125)
        self.connect('draw', self.draw_meter)

    def set_level(self, value):
        level = max(0.0, min(1.0, value))
        if round(level * 22) != round(self.level * 22):
            self.level = level
            self.queue_draw()

    def draw_meter(self, widget, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        cr.set_source_rgb(.16, .18, .19)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        count = round(self.level * 22)
        for i in range(22):
            color = ((.9, .27, .22) if i >= 19 else (.92, .73, .2) if i >= 15 else (.26, .72, .4)) if i < count else (.24, .27, .28)
            cr.set_source_rgb(*color)
            cr.rectangle(1, h - (i + 1) * h / 22 + 1, w - 2, max(1, h / 22 - 2))
            cr.fill()
        return False


class PeakMonitor:
    def __init__(self, key, device, callback):
        self.key, self.device, self.callback = key, device, callback
        self.stopping = threading.Event()
        self.process = None
        self._meter_lock = threading.Lock()
        self._pending_level = None
        self._idle_queued = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass

    def queue_level(self, level):
        # Keep at most one GTK callback pending per meter. Replace stale
        # readings instead of building a backlog during desktop load.
        with self._meter_lock:
            self._pending_level = level
            if self._idle_queued:
                return
            self._idle_queued = True
        GLib.idle_add(self.deliver_level, priority=GLib.PRIORITY_HIGH_IDLE)

    def deliver_level(self):
        with self._meter_lock:
            level = self._pending_level
            self._pending_level = None
            self._idle_queued = False
        if not self.stopping.is_set() and level is not None:
            self.callback(self.key, level)
        return False

    def run(self):
        kind, index = self.key
        if kind == 'sink':
            options = ['--device=' + self.device + '.monitor']
        elif kind == 'source':
            options = ['--device=' + self.device]
        elif kind == 'sink-input':
            options = ['--monitor-stream=' + str(index)]
        else:
            return
        cmd = ['parec', '--property=application.id=yuyo-peak-meter', '--raw', '--format=s16le', '--rate=16000', '--channels=1', '--latency-msec=100', *options]
        try:
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            last = 0
            while not self.stopping.is_set():
                data = self.process.stdout.read(3200)
                if not data:
                    break
                samples = array.array('h')
                samples.frombytes(data[:len(data) // 2 * 2])
                if samples:
                    peak = min(1.0, max(abs(x) for x in samples) / 32768.0)
                    level = max(0, min(1, (20 * math.log10(max(peak, .00001)) + 60) / 60))
                    now = time.monotonic()
                    if now - last >= .045:
                        self.queue_level(level)
                        last = now
        except (OSError, ValueError):
            pass
        finally:
            if self.process:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    try:
                        self.process.kill()
                    except OSError:
                        pass
                except OSError:
                    pass


class Channel(Gtk.Fixed):
    def __init__(self, item, report_error):
        super().__init__()
        self.set_size_request(COLUMN_WIDTH, ICON_ROW_HEIGHT + NAME_ROW_HEIGHT + SLIDER_ROW_HEIGHT + NUMBER_ROW_HEIGHT + DB_ROW_HEIGHT + MUTE_ROW_HEIGHT + TYPE_ROW_HEIGHT)
        self.set_hexpand(False)
        self.set_margin_top(5)
        self.set_margin_bottom(12)
        self.set_margin_start(4)
        self.set_margin_end(4)
        self.key = item['key']
        self.report_error = report_error
        self.updating = False
        self.muted = item['muted']
        self.name = item['name']
        self.last_volume = item['volume']
        self.last_detail = item['detail']
        self._volume_timeout = None

        self.icon_name = item['icon']
        image = themed_image(self.icon_name)
        self.icon_image = image
        icon_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        icon_slot.set_size_request(COLUMN_WIDTH, ICON_ROW_HEIGHT)
        icon_slot.set_halign(Gtk.Align.CENTER)
        icon_slot.set_valign(Gtk.Align.CENTER)
        image.set_halign(Gtk.Align.CENTER)
        image.set_valign(Gtk.Align.CENTER)
        icon_slot.pack_start(image, True, True, 0)
        self.icon_slot = icon_slot
        self.put(icon_slot, 0, 0)

        name = Gtk.Label(label=item['name'])
        name.set_max_width_chars(14)
        name.set_width_chars(14)
        name.set_ellipsize(3)
        name.set_lines(2)
        name.set_size_request(COLUMN_WIDTH - 12, NAME_ROW_HEIGHT)  # Reserve two lines for every channel.
        name.set_valign(Gtk.Align.CENTER)
        name.set_yalign(0.5)
        name.set_line_wrap(True)
        name.set_justify(Gtk.Justification.CENTER)
        name.set_tooltip_text(item['name'] + ('\n' + item['detail'] if item['detail'] else ''))
        name.set_halign(Gtk.Align.CENTER)
        self.put(name, 6, ICON_ROW_HEIGHT)
        self.name_label = name

        self.adjustment = Gtk.Adjustment(value=min(item['volume'], MAX_VOLUME), lower=0,
                                         upper=MAX_VOLUME, step_increment=1, page_increment=10)
        self.slider = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, adjustment=self.adjustment)
        self.slider.set_inverted(True)
        self.slider.set_draw_value(False)
        self.slider.set_size_request(-1, 125)
        self.slider.set_vexpand(False)
        slider_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        slider_slot.set_size_request(COLUMN_WIDTH, SLIDER_ROW_HEIGHT)
        slider_slot.set_halign(Gtk.Align.CENTER)
        slider_slot.pack_start(self.slider, True, False, 0)
        self.put(slider_slot, 0, ICON_ROW_HEIGHT + NAME_ROW_HEIGHT)
        self.meter = LevelMeter()
        self.put(self.meter, COLUMN_WIDTH // 2 + 22, ICON_ROW_HEIGHT + NAME_ROW_HEIGHT + 10)

        self.spin = Gtk.SpinButton(adjustment=self.adjustment, climb_rate=1, digits=0)
        self.spin.set_numeric(True)
        self.spin.set_width_chars(4)
        self.spin.set_alignment(0.5)
        self.spin.set_tooltip_text('Enter exact volume from 0% to 200%')
        percent_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        percent_row.set_halign(Gtk.Align.CENTER)
        percent_row.pack_start(self.spin, False, False, 0)
        percent_row.pack_start(Gtk.Label(label='%'), False, False, 0)
        number_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        number_slot.set_size_request(COLUMN_WIDTH, NUMBER_ROW_HEIGHT)
        number_slot.pack_start(percent_row, True, False, 0)
        self.put(number_slot, 0, ICON_ROW_HEIGHT + NAME_ROW_HEIGHT + SLIDER_ROW_HEIGHT)

        self.db_label = Gtk.Label()
        self.db_label.set_size_request(COLUMN_WIDTH, DB_ROW_HEIGHT)
        self.db_label.set_halign(Gtk.Align.CENTER)
        self.db_label.set_tooltip_text('Volume gain relative to 100% (0 dB)')
        self.put(self.db_label, 0, ICON_ROW_HEIGHT + NAME_ROW_HEIGHT + SLIDER_ROW_HEIGHT + NUMBER_ROW_HEIGHT)
        self._update_db(item['volume'])

        self.mute = Gtk.ToggleButton()
        self.mute.set_relief(Gtk.ReliefStyle.NONE)
        self.mute.set_tooltip_text('Mute or unmute')
        self.mute.set_active(item['muted'])
        self._set_mute_icon(item['muted'])
        mute_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        mute_slot.set_size_request(COLUMN_WIDTH, MUTE_ROW_HEIGHT)
        self.mute.set_halign(Gtk.Align.CENTER)
        mute_slot.pack_start(self.mute, True, False, 0)
        self.put(mute_slot, 0, ICON_ROW_HEIGHT + NAME_ROW_HEIGHT + SLIDER_ROW_HEIGHT + NUMBER_ROW_HEIGHT + DB_ROW_HEIGHT)
        # Type belongs below the controls, not in the device name.
        self.type_label = Gtk.Label()
        self.type_label.set_size_request(COLUMN_WIDTH, TYPE_ROW_HEIGHT)
        self.type_label.set_halign(Gtk.Align.CENTER)
        self.type_label.get_style_context().add_class('dim-label')
        self.type_label.set_text(self._source_type(item))
        self.put(self.type_label, 0, ICON_ROW_HEIGHT + NAME_ROW_HEIGHT + SLIDER_ROW_HEIGHT + NUMBER_ROW_HEIGHT + DB_ROW_HEIGHT + MUTE_ROW_HEIGHT)
        self.adjustment.connect('value-changed', self._on_volume)
        self.mute.connect('toggled', self._on_mute)

    @staticmethod
    def _source_type(item):
        if item['key'][0] != 'source':
            return ''
        return 'Playback Monitor' if str(item.get('device_name', '')).endswith('.monitor') else 'Microphone'

    def refresh_icon(self):
        # Re-resolve the icon in case the new theme uses a different name.
        replacement = themed_image(self.icon_name)
        self.icon_slot.remove(self.icon_image)
        self.icon_slot.pack_start(replacement, True, True, 0)
        replacement.show()
        self.icon_image = replacement
        self._set_mute_icon(self.muted)

    def _set_mute_icon(self, muted):
        if muted:
            icon = 'microphone-sensitivity-muted-symbolic' if self.key[0] in ('source', 'source-output') else 'audio-volume-muted-symbolic'
        else:
            icon = 'microphone-sensitivity-high-symbolic' if self.key[0] in ('source', 'source-output') else 'audio-volume-high-symbolic'
        image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DIALOG)
        image.set_pixel_size(24)
        self.mute.set_image(image)

    def _update_db(self, percent):
        db = 20 * math.log10(percent / 100.0) if percent > 0 else float('-inf')
        self.db_label.set_text(f'{db:+.1f} dB' if math.isfinite(db) else '-∞ dB')

    def _on_volume(self, adjustment):
        self._update_db(adjustment.get_value())
        if self.updating:
            return
        # Programmatic updates are blocked by self.updating. Slider and spin edits apply immediately.
        self.last_volume = round(adjustment.get_value())
        # Coalesce rapid slider motion into one pactl operation every 70 ms.
        if self._volume_timeout is not None:
            GLib.source_remove(self._volume_timeout)
        self._volume_timeout = GLib.timeout_add(70, self._commit_volume)

    def _commit_volume(self):
        self._volume_timeout = None
        try:
            apply_volume(self.key, self.last_volume)
        except (OSError, subprocess.CalledProcessError) as e:
            self.report_error(str(e))
        return False

    def _on_mute(self, button):
        if self.updating:
            return
        self.muted = button.get_active()
        self._set_mute_icon(self.muted)
        try:
            apply_mute(self.key, self.muted)
        except (OSError, subprocess.CalledProcessError) as e:
            self.report_error(str(e))

    def do_destroy(self):
        if self._volume_timeout is not None:
            GLib.source_remove(self._volume_timeout)
            self._volume_timeout = None
        Gtk.Fixed.do_destroy(self)

    def sync(self, item):
        self.type_label.set_text(self._source_type(item))
        # Preserve the existing widget, including slider focus and meter state.
        if item['name'] != self.name or item['detail'] != self.last_detail:
            self.name = item['name']
            self.last_detail = item['detail']
            self.name_label.set_text(self.name)
            self.name_label.set_tooltip_text(
                self.name + ('\\n' + self.last_detail if self.last_detail else ''))
        if item['icon'] != self.icon_name:
            self.icon_name = item['icon']
            self.refresh_icon()
        if item['volume'] != self.last_volume:
            self.last_volume = item['volume']
            if not (self.spin.has_focus() or self.slider.has_focus()):
                self.updating = True
                try:
                    self.adjustment.set_value(min(item['volume'], MAX_VOLUME))
                finally:
                    self.updating = False
        if item['muted'] != self.muted:
            self.updating = True
            try:
                self.muted = item['muted']
                self.mute.set_active(self.muted)
                self._set_mute_icon(self.muted)
            finally:
                self.updating = False


class AudioEventListener:
    """Subscribe to PulseAudio-compatible events without blocking GTK."""
    def __init__(self, notify):
        self.notify = notify
        self.stop_event = threading.Event()
        self.process = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.process = subprocess.Popen(
                    ['pactl', 'subscribe'], stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
                for line in self.process.stdout:
                    if self.stop_event.is_set():
                        break
                    # Sink/source, stream, device and default-device changes.
                    if any(f"on {kind} " in line for kind in
                           ('sink', 'source', 'sink-input', 'source-output',
                            'server', 'card')):
                        GLib.idle_add(self.notify)
                if self.stop_event.is_set():
                    break
            except OSError:
                pass
            finally:
                if self.process and self.process.poll() is None:
                    try:
                        self.process.terminate()
                    except OSError:
                        pass
                self.process = None
            self.stop_event.wait(3)


class Mixer(Gtk.Window):
    def __init__(self):
        super().__init__(title='Yuyo')
        self.set_default_size(560, 456)
        self.set_size_request(320, 290)
        self.connect('destroy', self.shutdown)
        self.connect('delete-event', self.on_close)
        self.connect('hide', self.on_visibility_change)
        self.connect('show', self.on_visibility_change)
        # MATE notification area supports GTK3 StatusIcon.
        self.tray = Gtk.StatusIcon.new_from_icon_name('audio-volume-high')
        self.tray.set_tooltip_text('Yuyo: click to show or hide')
        self.tray.set_visible(True)
        self.tray.connect('activate', self.on_tray_activate)
        self.tray.connect('popup-menu', self.on_tray_menu)
        self.tray.connect('scroll-event', self.on_tray_scroll)
        self.monitors = {}
        self.latest_groups = {}
        self.tabs = {}
        self.scrolls = {}
        self.channels = {}
        self.last_signature = {}
        self.counts = {'Output': 0, 'Input': 0}
        self.show_disabled_cards = True
        self.target_width = 560
        self.last_tray_state = None
        self._audio_snapshot = {}
        self._last_refresh_at = 0
        self._default_sink_name = None
        self._default_sink_checked = 0
        self._hardware_signature = None
        self.closing = False
        self.icon_theme = Gtk.IconTheme.get_default()
        self.icon_theme.connect('changed', self.on_icon_theme_changed)
        self.gtk_settings = Gtk.Settings.get_default()
        if self.gtk_settings is not None:
            self.gtk_settings.connect('notify::gtk-icon-theme-name',
                                      self.on_icon_theme_setting_changed)


        # Use the normal MATE/Marco title bar instead of a tall GTK HeaderBar.
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)
        notebook = Gtk.Notebook()
        self.notebook = notebook
        notebook.connect('switch-page', self.on_switch_page)
        # Space around the notebook so tabs and page edges do not touch the window.
        notebook.set_margin_top(12)
        notebook.set_margin_start(12)
        notebook.set_margin_end(12)
        notebook.set_margin_bottom(12)
        root.pack_start(notebook, True, True, 0)
        for tab in ('Output', 'Input'):
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
            self.scrolls[tab] = scroll
            box = Gtk.Grid()
            box.set_column_spacing(4)
            box.set_row_spacing(10)
            box.set_margin_start(9)
            box.set_margin_end(9)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            scroll.add(box)
            notebook.append_page(scroll, Gtk.Label(label=tab))
            self.tabs[tab] = box
            self.channels[tab] = {}
        # Card management is separate from the unchanged Output/Input mixer.
        hardware_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hardware_controls.set_margin_start(16)
        hardware_controls.set_margin_top(8)
        self.disabled_toggle = Gtk.CheckButton(label='Show disabled audio cards')
        self.disabled_toggle.set_active(True)
        self.disabled_toggle.connect('toggled', self.on_disabled_toggle)
        hardware_controls.pack_start(self.disabled_toggle, False, False, 0)
        hardware_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hardware_page.pack_start(hardware_controls, False, False, 0)
        hardware_scroll = Gtk.ScrolledWindow()
        hardware_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.hardware_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.hardware_list.set_margin_start(16)
        self.hardware_list.set_margin_end(16)
        self.hardware_list.set_margin_top(12)
        self.hardware_list.set_margin_bottom(12)
        hardware_scroll.add(self.hardware_list)
        hardware_page.pack_start(hardware_scroll, True, True, 0)
        notebook.append_page(hardware_page, Gtk.Label(label='Hardware'))
        self.message = Gtk.Label(label='')
        self.message.set_xalign(0)
        self.message.set_margin_start(12)
        self.message.set_margin_bottom(0)
        self.message.set_no_show_all(True)
        self.message.hide()
        root.pack_end(self.message, False, False, 0)
        self.refresh(rebuild=True)
        self.refresh_hardware()
        # Coalesce bursts of server events into a single UI refresh.
        self.pending_refresh = None
        self.event_listener = AudioEventListener(self.schedule_refresh)
        # Slow fallback handles missed events and reconnects.
        GLib.timeout_add_seconds(15, self.poll)

    def shutdown(self, *args):
        self.closing = True
        self.event_listener.stop()
        if self.pending_refresh is not None:
            GLib.source_remove(self.pending_refresh)
            self.pending_refresh = None
        for monitor in self.monitors.values():
            monitor.stop()
        self.monitors.clear()
        Gtk.main_quit()

    def on_visibility_change(self, *args):
        # No audio capture processes while the mixer is hidden in the tray.
        GLib.idle_add(self.sync_monitors)


    def on_meter_sample(self, key, level):
        for channels in self.channels.values():
            if key in channels:
                channels[key].meter.set_level(level)
        return False

    def sync_monitors(self, page_number=None):
        if page_number is None:
            page_number = self.notebook.get_current_page()
        active = 'Input' if page_number == 1 else 'Output'
        wanted = ({item['key']: item for item in self.latest_groups.get(active, [])
                   if item['key'][0] in ('sink', 'source', 'sink-input')}
                  if self.get_visible() and page_number in (0, 1) else {})
        for key in list(self.monitors):
            if key not in wanted:
                self.monitors.pop(key).stop()
        if shutil.which('parec'):
            for key, item in wanted.items():
                if key not in self.monitors:
                    self.monitors[key] = PeakMonitor(key, item['device_name'], self.on_meter_sample)

    def on_close(self, window, event):
        # Closing the window keeps the mixer available in the system tray.
        self.hide()
        return True

    def on_tray_activate(self, icon):
        if self.get_visible():
            self.hide()
        else:
            self.show_all()
            self.present()

    def default_output(self):
        name = pulse('get-default-sink').strip()
        for item in get_streams('sinks', self._audio_snapshot.get('sinks')):
            if item['device_name'] == name:
                return item
        return None

    def change_default_volume(self, delta):
        try:
            item = self.default_output()
            if item:
                apply_volume(item['key'], max(0, min(MAX_VOLUME, item['volume'] + delta)))
                self.refresh_tray()
                self.refresh()
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            self.error(str(exc))

    def toggle_default_mute(self, *args):
        try:
            item = self.default_output()
            if item:
                apply_mute(item['key'], not item['muted'])
                self.refresh_tray()
                self.refresh()
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            self.error(str(exc))

    def refresh_tray(self):
        try:
            if self._audio_snapshot.get('sinks') is not None and time.monotonic() - self._last_refresh_at < 2:
                if self._default_sink_name is None or time.monotonic() - self._default_sink_checked > 2:
                    self._default_sink_name = pulse('get-default-sink').strip()
                    self._default_sink_checked = time.monotonic()
                item = next((i for i in get_streams('sinks', self._audio_snapshot['sinks'])
                             if i['device_name'] == self._default_sink_name), None)
            else:
                item = self.default_output()
            if not item:
                return
            volume = item['volume']
            muted = item['muted']
            icon_name = ('audio-volume-muted' if muted or volume == 0 else
                         'audio-volume-low' if volume < 35 else
                         'audio-volume-medium' if volume < 70 else
                         'audio-volume-high')
            state = 'Muted' if muted else f'{volume}%'
            if (icon_name, state) != self.last_tray_state:
                self.tray.set_from_icon_name(icon_name)
                self.tray.set_tooltip_text(f'Yuyo: {state}\\nScroll to adjust volume')
                self.last_tray_state = (icon_name, state)
        except (OSError, subprocess.CalledProcessError, ValueError):
            pass

    def on_tray_scroll(self, icon, event):
        if event.direction == Gdk.ScrollDirection.UP:
            self.change_default_volume(5)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self.change_default_volume(-5)
        return True

    def open_tab(self, tab):
        self.notebook.set_current_page(0 if tab == 'Output' else 1)
        self.show_all()
        self.present()

    def on_tray_menu(self, icon, button, activate_time):
        menu = Gtk.Menu()
        menu.get_style_context().add_class('mixer-tray-menu')
        css = Gtk.CssProvider()
        css.load_from_data(b'menu.mixer-tray-menu { padding: 8px 14px; }')
        menu.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        def add_item(label, callback, icon_name=None):
            item = Gtk.ImageMenuItem.new_with_label(label)
            item.set_always_show_image(True)
            if icon_name:
                image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
                item.set_image(image)
            item.connect('activate', callback)
            menu.append(item)
            return item

        add_item('Show / Hide Mixer', lambda *_: self.on_tray_activate(icon), 'view-restore')
        add_item('Output', lambda *_: self.open_tab('Output'), 'audio-speakers')
        add_item('Input', lambda *_: self.open_tab('Input'), 'audio-input-microphone')
        menu.append(Gtk.SeparatorMenuItem())
        try:
            output = self.default_output()
        except (OSError, subprocess.CalledProcessError, ValueError):
            output = None
        if output:
            info = Gtk.MenuItem(label=f"Output volume: {output['volume']}%")
            info.set_sensitive(False)
            menu.append(info)
            add_item('Volume +5%', lambda *_: self.change_default_volume(5), 'list-add')
            add_item('Volume -5%', lambda *_: self.change_default_volume(-5), 'list-remove')
            add_item('Unmute Output' if output['muted'] else 'Mute Output',
                     self.toggle_default_mute,
                     'audio-volume-high' if output['muted'] else 'audio-volume-muted')
        menu.append(Gtk.SeparatorMenuItem())
        add_item('Quit', lambda *_: self.shutdown(), 'application-exit')
        menu.show_all()
        menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, activate_time)

    def on_icon_theme_setting_changed(self, settings, param):
        self.on_icon_theme_changed(self.icon_theme)

    def on_icon_theme_changed(self, theme):
        # Theme changes need to update already-created widgets, not only
        # newly created controls. Keep volume meters and sliders intact.
        for channels in self.channels.values():
            for channel in channels.values():
                channel.refresh_icon()
        self._hardware_dirty = True
        self.refresh_hardware(force=True)
        # Re-apply the icon name so the tray redraws with the new theme.
        self.last_tray_state = None
        self.refresh_tray()

    def error(self, message):
        self.message.set_text(message[:180])
        self.message.show()

    def refresh(self, rebuild=False):
        try:
            raw = {kind: json.loads(pulse('-f', 'json', 'list', kind))
                   for kind in ('sinks', 'sink-inputs', 'sources', 'source-outputs')}
            groups = {
                'Output': get_streams('sinks', raw['sinks']) + get_streams('sink-inputs', raw['sink-inputs']),
                'Input': get_streams('sources', raw['sources']) + get_streams('source-outputs', raw['source-outputs'])
            }
            self._audio_snapshot = raw
            self._last_refresh_at = time.monotonic()
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as e:
            self.error('Unable to read audio streams: ' + str(e))
            return
        self.latest_groups = groups
        for tab, items in groups.items():
            self.counts[tab] = len(items)
            box = self.tabs[tab]
            channels = self.channels[tab]
            wanted_keys = {item['key'] for item in items}

            # Remove only channels which have disappeared.
            for key in list(channels):
                if key not in wanted_keys:
                    channel = channels.pop(key)
                    box.remove(channel)

            # Reuse widgets for unchanged streams. Only new streams get widgets.
            for position, item in enumerate(items):
                key = item['key']
                channel = channels.get(key)
                if channel is None:
                    channel = Channel(item, self.error)
                    channels[key] = channel
                    box.attach(channel, position % MAX_VISIBLE_COLUMNS,
                               position // MAX_VISIBLE_COLUMNS, 1, 1)
                    channel.show_all()
                else:
                    channel.sync(item)
            # Reflow only when channel count/order changes. Keep widgets and
            # live meters intact, and wrap overflow to another row.
            layout = tuple(item['key'] for item in items)
            if getattr(self, '_grid_layout_' + tab, None) != layout:
                for widget in list(box.get_children()):
                    box.remove(widget)
                for position, item in enumerate(items):
                    widget = channels[item['key']]
                    box.attach(widget, position % MAX_VISIBLE_COLUMNS,
                               position // MAX_VISIBLE_COLUMNS, 1, 1)
                box.show_all()
                setattr(self, '_grid_layout_' + tab, layout)

            self.last_signature[tab] = tuple(
                (item['key'], item['name'], item['icon']) for item in items)
        self.message.set_text('')
        self.message.hide()
        if self.notebook.get_current_page() == 2:
            self.resize_for_hardware()
        else:
            self.resize_for_tab()
        self.sync_monitors()
        self.refresh_tray()
        if self.notebook.get_current_page() == 2:
            self.refresh_hardware()

    def on_switch_page(self, notebook, page, page_number):
        self.sync_monitors(page_number)
        if page_number == 2:
            self.refresh_hardware()
            GLib.idle_add(self.resize_for_hardware)
        else:
            GLib.idle_add(self.resize_for_tab)

    def set_hardware_default(self, device):
        if device.get('is_monitor'):
            return
        command = 'set-default-sink' if device['direction'] == 'Playback' else 'set-default-source'
        try:
            pulse(command, device['id'])
            # pactl changes the default for this direction, replacing the
            # previous default. Refresh all card badges from the server.
            self.refresh_hardware()
            self.refresh()
            # PipeWire/WirePlumber can publish the final default asynchronously.
            GLib.timeout_add(350, self.refresh_defaults_after_change)
        except (OSError, subprocess.CalledProcessError) as exc:
            self.error(str(exc))

    def refresh_defaults_after_change(self):
        if not self.closing:
            self.refresh_hardware()
        return False

    def disable_hardware(self, device):
        # A PulseAudio card profile is card-wide, never an independent endpoint switch.
        try:
            card = next((c for c in audio_cards() if c.get('index') == device['card']), None)
            if not card:
                self.error('This audio endpoint has no associated PulseAudio card. '
                           'Card-level disabling is unavailable for this device.')
                return
            active = card.get('active_profile')
            profile = active.get('name') if isinstance(active, dict) else active
            if not profile or profile == 'off':
                return
            dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK_CANCEL,
                text='Disable the entire audio card?')
            dialog.format_secondary_text(
                'This affects every playback and recording endpoint on the card. '
                'To re-enable it, select a profile from the right-click menu.')
            confirmed = dialog.run() == Gtk.ResponseType.OK
            dialog.destroy()
            if not confirmed:
                return
            pulse('set-card-profile', str(card['index']), 'off')
            self.refresh_hardware()
            self.refresh()
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            self.error(str(exc))

    def enable_hardware_card(self, card, profile):
        """Apply a profile selected from the live PipeWire card list."""
        try:
            pulse('set-card-profile', str(card['index']), profile)
            self.refresh_hardware()
            self.refresh()
            GLib.timeout_add(350, self.refresh_defaults_after_change)
        except (OSError, subprocess.CalledProcessError) as exc:
            self.error('Unable to enable audio card: ' + str(exc))

    @staticmethod
    def card_profile(card):
        active = card.get('active_profile')
        return active.get('name', 'off') if isinstance(active, dict) else (active or 'off')

    @staticmethod
    def available_card_profiles(card):
        profiles = card.get('profiles') or {}
        options = []
        if isinstance(profiles, dict):
            entries = [(name, info) for name, info in profiles.items()]
        else:
            entries = [(info.get('name', ''), info) for info in profiles if isinstance(info, dict)]
        for name, info in entries:
            if not name or name == 'off':
                continue
            if isinstance(info, dict):
                availability = str(info.get('available', 'unknown')).lower()
                if availability in ('no', 'false', '0'):
                    continue
                label = info.get('description') or name
            else:
                label = name
            options.append((str(name), str(label)))
        return sorted(options, key=lambda x: x[1].casefold())

    def card_menu(self, widget, event, card, endpoints):
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        disabled = self.card_profile(card) == 'off'
        if not disabled:
            default_item = Gtk.MenuItem(label='Set as Default')
            submenu = Gtk.Menu()
            eligible = [d for d in endpoints if not d['is_monitor']]
            for device in sorted(eligible, key=lambda d: (d['direction'], d['name'])):
                item = Gtk.MenuItem(label=f"{device['direction']}: {device['name']}")
                item.connect('activate', lambda _item, d=device: self.set_hardware_default(d))
                submenu.append(item)
            if not eligible:
                empty = Gtk.MenuItem(label='No available playback or microphone channels')
                empty.set_sensitive(False)
                submenu.append(empty)
            default_item.set_submenu(submenu)
            menu.append(default_item)
            menu.append(Gtk.SeparatorMenuItem())
        action = Gtk.MenuItem(label='Enable entire audio card' if disabled else 'Disable entire audio card...')
        if disabled:
            profiles = self.available_card_profiles(card)
            submenu = Gtk.Menu()
            for profile_name, description in profiles:
                item = Gtk.MenuItem(label=description)
                item.set_tooltip_text(profile_name)
                item.connect('activate', lambda _item, name=profile_name: self.enable_hardware_card(card, name))
                submenu.append(item)
            action.set_sensitive(bool(profiles))
            if profiles:
                action.set_submenu(submenu)
        else:
            action.connect('activate', lambda *_: self.disable_hardware({'card': card['index']}))
        menu.append(action)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_disabled_toggle(self, button):
        self.show_disabled_cards = button.get_active()
        self.refresh_hardware(force=True)
        if self.notebook.get_current_page() == 2:
            GLib.idle_add(self.resize_for_hardware)

    def resize_for_hardware(self):
        return self.resize_for_tab()

    def refresh_hardware(self, force=False):
        try:
            cards = audio_cards()
            endpoints = hardware_devices(cards, self._audio_snapshot.get('sinks'),
                                         self._audio_snapshot.get('sources'))
            defaults = {'Playback': pulse('get-default-sink').strip(),
                        'Recording': pulse('get-default-source').strip()}
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            self.error('Unable to read hardware: ' + str(exc))
            return
        # Preserve GTK cards and scroll position if nothing on Hardware changed.
        signature = (json.dumps(cards, sort_keys=True), json.dumps(endpoints, sort_keys=True),
                     tuple(sorted(defaults.items())), self.show_disabled_cards)
        if not force and signature == self._hardware_signature:
            return
        self._hardware_signature = signature
        scroll = self.hardware_list.get_parent()
        adjustment = scroll.get_vadjustment() if isinstance(scroll, Gtk.Viewport) else None
        old_scroll = adjustment.get_value() if adjustment else 0
        for child in self.hardware_list.get_children():
            self.hardware_list.remove(child)

        def card_sort_key(card):
            card_channels = [d for d in endpoints
                             if str(d['card']) == str(card['index'])]
            disabled = self.card_profile(card) == 'off'
            playback_default = any(
                d['direction'] == 'Playback' and d['id'] == defaults['Playback']
                for d in card_channels)
            recording_default = any(
                d['direction'] == 'Recording' and d['id'] == defaults['Recording']
                for d in card_channels)
            name = (card.get('properties') or {}).get('device.description',
                                                    card.get('name', ''))
            # Both defaults first, then playback default, recording default,
            # other enabled cards, and finally disabled cards.
            priority = (0 if playback_default and recording_default else
                        1 if playback_default else
                        2 if recording_default else
                        4 if disabled else 3)
            return (priority, str(name).casefold())

        def channel_sort_key(device):
            direction = device['direction']
            is_default = (direction in defaults and
                          device['id'] == defaults[direction])
            priority = (0 if is_default and direction == 'Playback' else
                        1 if is_default and direction == 'Recording' else
                        4 if direction == 'Playback Monitor' else
                        2 if direction == 'Playback' else 3)
            return (priority, str(device['name']).casefold())

        for card in sorted(cards, key=card_sort_key):
            props = card.get('properties') or {}
            name = (props.get('device.description') or
                    props.get('alsa.card_name') or card.get('name', 'Audio card'))
            disabled = self.card_profile(card) == 'off'
            if disabled and not self.show_disabled_cards:
                continue
            channels = [d for d in endpoints if str(d['card']) == str(card['index'])]

            frame = Gtk.Frame()
            outer = Gtk.EventBox()
            frame.add(outer)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            body.set_border_width(12)
            outer.add(body)

            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            icon = themed_image('audio-card', 36)
            if disabled:
                icon.set_opacity(.45)
            header.pack_start(icon, False, False, 0)
            title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            title = Gtk.Label(label=str(name))
            title.set_xalign(0)
            title.set_line_wrap(True)
            title.set_selectable(False)
            title_box.pack_start(title, False, False, 0)
            description = (props.get('device.product.name') or
                           props.get('device.bus') or card.get('name', ''))
            # Card status belongs to the card; default status belongs to
            # individual playback/recording channels.
            if str(description).strip().casefold() != str(name).strip().casefold():
                sub = Gtk.Label()
                sub.set_markup('<span size="small">' +
                               GLib.markup_escape_text(str(description)) +
                               '</span>')
                sub.set_xalign(0)
                sub.set_line_wrap(True)
                sub.get_style_context().add_class('dim-label')
                title_box.pack_start(sub, False, False, 0)

            state_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            status_icon = Gtk.Image.new_from_icon_name(
                'process-stop' if disabled else 'emblem-default',
                Gtk.IconSize.MENU)
            status_icon.set_pixel_size(24)
            state_row.pack_start(status_icon, False, False, 0)
            status_label = Gtk.Label()
            status_label.set_markup('<span size="small">' +
                                    ('Disabled' if disabled else 'Enabled') +
                                    '</span>')
            state_row.pack_start(status_label, False, False, 0)
            title_box.pack_start(state_row, False, False, 0)
            if disabled:
                title.get_style_context().add_class('dim-label')
            header.pack_start(title_box, True, True, 0)
            body.pack_start(header, False, False, 0)

            if channels:
                channel_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                for device in sorted(channels, key=channel_sort_key):
                    direction = device['direction']
                    default = (direction in defaults and
                               device['id'] == defaults[direction])
                    kind = GLib.markup_escape_text(direction)
                    label_name = GLib.markup_escape_text(str(device['name']))
                    detail_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
                    detail_row.set_margin_start(48)
                    # Reserve the same 20px icon slot on every row, so
                    # Playback Monitor aligns with default channel labels.
                    if default:
                        channel_icon = Gtk.Image.new_from_icon_name(
                            'emblem-favorite', Gtk.IconSize.MENU)
                        channel_icon.set_pixel_size(20)
                        markup = ('<span size="small"><b>' + kind +
                                  ' (Default)</b>: ' + label_name + '</span>')
                    else:
                        channel_icon = Gtk.Box()
                        channel_icon.set_size_request(20, 20)
                        markup = ('<span size="small">' + kind + ': ' +
                                  label_name + '</span>')
                    detail_row.pack_start(channel_icon, False, False, 0)
                    detail = Gtk.Label()
                    detail.set_markup(markup)
                    detail.set_line_wrap(True)
                    detail.set_max_width_chars(38)
                    detail.set_xalign(0)
                    # Align channel text with the card title and description,
                    # to the right of the 36px icon and 12px header gap.
                    detail_row.set_margin_top(2)
                    detail_row.set_margin_bottom(2)
                    detail_row.pack_start(detail, True, True, 0)
                    channel_box.pack_start(detail_row, False, False, 0)
                body.pack_start(channel_box, False, False, 0)
            else:
                hint = 'Channels disabled' if disabled else 'No active channels'
                empty = Gtk.Label()
                empty.set_markup('<span size="small">' + GLib.markup_escape_text(hint) + '</span>')
                empty.set_xalign(0)
                empty.set_margin_start(48)
                empty.get_style_context().add_class('dim-label')
                body.pack_start(empty, False, False, 0)

            # Right-click anywhere on the card to manage it.
            outer.connect('button-press-event', self.card_menu, card, channels)
            self.hardware_list.pack_start(frame, False, False, 0)

        self.hardware_list.show_all()
        if adjustment:
            GLib.idle_add(lambda: adjustment.set_value(min(old_scroll, max(0, adjustment.get_upper() - adjustment.get_page_size()))) or False)

    def resize_for_tab(self):
        # Size for the busier audio tab. Up to five channels across;
        # additional channels wrap below instead of creating a horizontal bar.
        count = max(self.counts.values(), default=0)
        columns = max(1, min(MAX_VISIBLE_COLUMNS, count))
        desired = max(MIN_WINDOW_WIDTH,
                      columns * (COLUMN_WIDTH + 4) + WINDOW_SIDE_SPACE)
        rows = max(1, (count + MAX_VISIBLE_COLUMNS - 1) // MAX_VISIBLE_COLUMNS)
        # Audio tabs never scroll. Grow vertically for wrapped rows, while
        # keeping the window within the usable monitor height.
        wanted_height = 456 + (rows - 1) * 410
        screen = self.get_screen()
        if screen:
            monitor = screen.get_monitor_at_window(self.get_window()) if self.get_window() else screen.get_primary_monitor()
            if monitor < 0:
                monitor = 0
            area = screen.get_monitor_workarea(monitor)
            wanted_height = min(wanted_height, max(456, area.height - 64))
        return self._apply_window_size(desired, wanted_height)

    def _apply_window_size(self, desired, height):
        if getattr(self, '_last_requested_size', None) != (desired, height):
            self.target_width = desired
            self._last_requested_size = (desired, height)
            self.resize(desired, height)
        return False

    def schedule_refresh(self):
        self._default_sink_name = None
        if not self.closing and self.pending_refresh is None:
            self.pending_refresh = GLib.timeout_add(120, self.flush_refresh)
        return False

    def flush_refresh(self):
        self.pending_refresh = None
        if not self.closing:
            self.refresh()
        return False

    def poll(self):
        if self.closing:
            return False
        self.schedule_refresh()
        return True


if __name__ == '__main__':
    if not shutil.which('pactl'):
        sys.exit('Missing pactl. Install with: sudo dnf install pulseaudio-utils')
    # An advisory lock prevents multiple background tray instances.
    lock_path = os.path.join(os.environ.get('XDG_RUNTIME_DIR', tempfile.gettempdir()),
                             'yuyo.lock')
    lock_file = open(lock_path, 'w')
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Yuyo is already running. Use its system tray icon.')
        sys.exit(0)
    mixer = Mixer()
    if '--tray' in sys.argv[1:]:
        # Initialize the tray without opening the mixer window.
        mixer.show_all()
        mixer.hide()
    else:
        mixer.show_all()
    Gtk.main()