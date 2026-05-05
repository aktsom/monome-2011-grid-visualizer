"""state.py — shared mutable state between Flask thread and asyncio loop.

All values are plain module-level globals. Python's GIL makes reads and writes
of simple types (float, str, int, bool) atomic, so no lock is needed.
The grids dict is protected by a lock because dict mutation is not atomic.
"""
import json
import os
import threading
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG = os.path.join(_DIR, 'config.json')

# ── hot path — read 60x/sec by render loop ────────────────────────────────────
gain          = 8.0
preset        = '01 spectrum'

# ── audio / grid config — written rarely by Flask, read by main loop ──────────
audio_device  = 70      # device index; None = system default
restart_audio = False   # set True by UI to trigger stream restart
reconnect_grid = None   # set to port int by UI to trigger grid reconnect
active_grid   = None    # id of currently connected grid
last_heartbeat = time.time()  # updated by browser ping; 0 = never received

# ── grids dict — protected by lock (dict mutation not atomic) ─────────────────
_lock = threading.Lock()
_grids = {}  # {id: {'port': int, 'type': str}}


def get_grids():
    with _lock:
        return dict(_grids)


def add_grid(id, port, type):
    with _lock:
        _grids[id] = {'port': port, 'type': type}


PRESETS = ['01 spectrum', '02 spectrum 8', '03 flame', '04 lissajous', '05 beat pulse', '06 spectrum peak', '07 waveform']
preset_gain = {p: 8.0 for p in PRESETS}
preset_gain['03 flame'] = 3.0

_TRIM_DEFAULTS = {
    '01 spectrum':      [0.7, 0.7, 0.8, 0.9, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    '02 spectrum 8':    [0.70, 0.85, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    '06 spectrum peak': [1.0,  1.0,  1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
}
band_trim = {k: list(v) for k, v in _TRIM_DEFAULTS.items()}

_LED_ROLE_DEFAULTS = {
    '01 spectrum':      {'bar': 15, 'tip': 8},
    '02 spectrum 8':    {'bar': 15, 'tip': 8},
    '06 spectrum peak': {'bar': 15, 'tip': 8, 'peak': 3},
}
led_roles = {k: dict(v) for k, v in _LED_ROLE_DEFAULTS.items()}

_COL_BRIGHTNESS_SIZES = {'01 spectrum': 16, '02 spectrum 8': 8, '06 spectrum peak': 16}
col_brightness = {k: [1.0] * n for k, n in _COL_BRIGHTNESS_SIZES.items()}


def load_config():
    """Load persisted settings from config.json (called once at startup)."""
    global audio_device, gain, preset, band_trim, preset_gain, led_roles, col_brightness
    try:
        with open(_CONFIG) as f:
            c = json.load(f)
        if 'audio_device' in c:
            audio_device = c['audio_device']   # may be int or None
        if 'preset' in c and c['preset'] in PRESETS:
            preset = c['preset']
        if 'preset_gain' in c and isinstance(c['preset_gain'], dict):
            for k, v in c['preset_gain'].items():
                if k in preset_gain:
                    preset_gain[k] = float(v)
        gain = preset_gain[preset]
        if 'band_trim' in c and isinstance(c['band_trim'], dict):
            for k, v in c['band_trim'].items():
                if k in band_trim and len(v) == len(band_trim[k]):
                    band_trim[k] = [float(x) for x in v]
        if 'led_roles' in c and isinstance(c['led_roles'], dict):
            for k, v in c['led_roles'].items():
                if k in led_roles and isinstance(v, dict):
                    for role in led_roles[k]:
                        if role in v:
                            led_roles[k][role] = max(1, min(15, int(v[role])))
        if 'col_brightness' in c and isinstance(c['col_brightness'], dict):
            for k, v in c['col_brightness'].items():
                if k in col_brightness and len(v) == len(col_brightness[k]):
                    col_brightness[k] = [float(x) for x in v]
        print(f"config loaded (device={audio_device}, gain={gain}, preset={preset})")
    except FileNotFoundError:
        pass  # first run — no config yet
    except Exception as e:
        print(f"config load error: {e}")


def save_config():
    """Persist current settings to config.json."""
    try:
        with open(_CONFIG, 'w') as f:
            json.dump({
                'audio_device':   audio_device,
                'preset':         preset,
                'preset_gain':    preset_gain,
                'band_trim':      band_trim,
                'led_roles':      led_roles,
                'col_brightness': col_brightness,
            }, f)
    except Exception as e:
        print(f"config save error: {e}")


def snapshot():
    """Return a copy of all state safe to serialise to JSON."""
    return {
        'gain':         gain,
        'preset':       preset,
        'audio_device': audio_device,
        'grids':        get_grids(),
        'active_grid':  active_grid,
    }
