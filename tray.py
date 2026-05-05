"""tray.py — system tray icon for the monome visualizer."""
import os
import plistlib
import sys
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw

_APP_NAME = 'monome visualizer'
_DIR      = os.path.dirname(os.path.abspath(__file__))
_MAIN_PY  = os.path.join(_DIR, 'main.py')


def _make_icon():
    img  = Image.new('RGBA', (32, 32), (0, 0, 0, 255))
    draw = ImageDraw.Draw(img)
    dot, gap = 4, 3
    grid     = 4
    step     = dot + gap
    offset   = (32 - grid * step + gap) // 2
    for row in range(grid):
        for col in range(grid):
            x = offset + col * step
            y = offset + row * step
            draw.rectangle([x, y, x + dot - 1, y + dot - 1], fill=(180, 180, 180, 255))
    return img


# ── startup helpers (platform-specific) ──────────────────────────────────────

if sys.platform == 'win32':
    import winreg
    _REG_KEY    = r'Software\Microsoft\Windows\CurrentVersion\Run'
    _LAUNCH_VBS = os.path.join(_DIR, 'launch.vbs')

    def _is_startup_enabled():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as key:
                winreg.QueryValueEx(key, _APP_NAME)
                return True
        except FileNotFoundError:
            return False

    def _set_startup(enable):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY,
                            access=winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ,
                                  f'wscript.exe "{_LAUNCH_VBS}"')
            else:
                try:
                    winreg.DeleteValue(key, _APP_NAME)
                except FileNotFoundError:
                    pass

elif sys.platform == 'darwin':
    _PLIST_DIR  = os.path.expanduser('~/Library/LaunchAgents')
    _PLIST_PATH = os.path.join(_PLIST_DIR, 'com.monome.visualizer.plist')

    def _is_startup_enabled():
        return os.path.exists(_PLIST_PATH)

    def _set_startup(enable):
        if enable:
            os.makedirs(_PLIST_DIR, exist_ok=True)
            plist = {
                'Label':           'com.monome.visualizer',
                'ProgramArguments': [sys.executable, _MAIN_PY],
                'RunAtLoad':       True,
            }
            with open(_PLIST_PATH, 'wb') as f:
                plistlib.dump(plist, f)
        else:
            try:
                os.remove(_PLIST_PATH)
            except FileNotFoundError:
                pass

else:  # Linux
    _AUTOSTART_DIR  = os.path.expanduser('~/.config/autostart')
    _DESKTOP_PATH   = os.path.join(_AUTOSTART_DIR, 'monome-visualizer.desktop')

    def _is_startup_enabled():
        return os.path.exists(_DESKTOP_PATH)

    def _set_startup(enable):
        if enable:
            os.makedirs(_AUTOSTART_DIR, exist_ok=True)
            with open(_DESKTOP_PATH, 'w') as f:
                f.write(
                    '[Desktop Entry]\n'
                    'Type=Application\n'
                    f'Name={_APP_NAME}\n'
                    f'Exec={sys.executable} {_MAIN_PY}\n'
                    'Hidden=false\n'
                    'NoDisplay=false\n'
                    'X-GNOME-Autostart-enabled=true\n'
                )
        else:
            try:
                os.remove(_DESKTOP_PATH)
            except FileNotFoundError:
                pass


# ── tray ──────────────────────────────────────────────────────────────────────

def run_tray(stop_event: threading.Event, loop=None):
    icon_image = _make_icon()

    def on_open(icon, item):
        webbrowser.open('http://localhost:5000')

    def on_startup_toggle(icon, item):
        _set_startup(not _is_startup_enabled())
        icon.update_menu()

    def startup_checked(item):
        return _is_startup_enabled()

    def on_quit(icon, item):
        stop_event.set()
        if loop:
            loop.call_soon_threadsafe(loop.stop)
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem('Open UI', on_open, default=True),
        pystray.MenuItem('Start with system', on_startup_toggle, checked=startup_checked),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Quit', on_quit),
    )

    icon = pystray.Icon(_APP_NAME, icon_image, _APP_NAME, menu=menu)
    icon.run()
