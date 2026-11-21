"""Find the monitors and open a page full-screen on one of them (e.g. a projector as second screen).

    for s in monitors(): print(s)                      # Screen(index=0, x=0, y=0, width=1920, height=1080, primary=True)
    open_fullscreen('http://localhost:8081/', pick_screen(monitors(), None))

Monitors are listed on Windows (Win32 API), on macOS (AppKit via osascript) and on Linux with xrandr; elsewhere the list is empty and the page opens
on the main screen (drag it over and press F11). The page opens in Edge or Chrome as a kiosk window: no bars, no
tabs; Alt+F4 closes it.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass


@dataclass
class Screen:
    index: int
    x: int
    y: int
    width: int
    height: int
    primary: bool


def monitors():
    try:
        if sys.platform == 'win32':
            return _windows_monitors()
        if sys.platform == 'darwin':
            return _mac_monitors(subprocess.run(['osascript', '-l', 'JavaScript', '-e', _MAC_SCREENS],
                                                capture_output=True, text=True, timeout=10).stdout)
        if shutil.which('xrandr'):
            return _xrandr_monitors(subprocess.run(['xrandr', '--query'], capture_output=True, text=True).stdout)
    except Exception:  # noqa: BLE001 - no monitor list is fine, the page then opens on the main screen
        pass
    return []


def pick_screen(screens, index):
    """The given screen, or by default the first one that is not the primary (the projector)."""
    if index is not None:
        if not 0 <= index < len(screens):
            raise SystemExit(f'there is no screen {index}; found: ' + (', '.join(map(str, screens)) or 'none'))
        return screens[index]
    return next((s for s in screens if not s.primary), screens[0] if screens else None)


def _windows_monitors():
    import ctypes
    from ctypes import wintypes

    try:
        ctypes.windll.user32.SetProcessDPIAware()  # real pixels, not scaled ones (Edge positions windows in those)
    except (AttributeError, OSError):
        pass

    class MONITORINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT),
                    ('dwFlags', wintypes.DWORD)]

    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                              wintypes.LPARAM)

    def callback(hmon, _hdc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcMonitor
        found.append((r.left, r.top, r.right - r.left, r.bottom - r.top, bool(info.dwFlags & 1)))
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc(callback), 0)
    return [Screen(i, *m) for i, m in enumerate(sorted(found, key=lambda m: (m[0], m[1])))]


# NSScreen frames in points, origin bottom left of the main screen (screens[0]), y up; one line per screen.
_MAC_SCREENS = '''ObjC.import("AppKit"); var s = $.NSScreen.screens, out = [];
for (var i = 0; i < s.count; i++) { var f = s.objectAtIndex(i).frame;
  out.push([f.origin.x, f.origin.y, f.size.width, f.size.height].join(" ")); } out.join("\\n")'''


def _mac_monitors(text):
    """osascript output -> screens with Chrome's coordinates (points, origin top left of the main screen, y down)."""
    frames = [tuple(round(float(v)) for v in line.split()) for line in text.splitlines() if line.strip()]
    if not frames:
        return []
    main_height = frames[0][3]
    out = [(x, main_height - (y + h), w, h, i == 0) for i, (x, y, w, h) in enumerate(frames)]
    return [Screen(i, *m) for i, m in enumerate(sorted(out, key=lambda m: (m[0], m[1])))]


def _xrandr_monitors(text):
    out = []
    for m in re.finditer(r' connected (primary )?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', text):
        out.append((int(m[4]), int(m[5]), int(m[2]), int(m[3]), bool(m[1])))
    return [Screen(i, *m) for i, m in enumerate(sorted(out, key=lambda m: (m[0], m[1])))]


def find_browser():
    names = ['msedge', 'chrome', 'google-chrome', 'chromium', 'chromium-browser']
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    candidates = []
    for base in (os.environ.get('PROGRAMFILES(X86)'), os.environ.get('PROGRAMFILES'), os.environ.get('LOCALAPPDATA')):
        if base:
            candidates += [pathlib.Path(base, 'Microsoft/Edge/Application/msedge.exe'),
                           pathlib.Path(base, 'Google/Chrome/Application/chrome.exe')]
    candidates.append(pathlib.Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'))
    return next((str(c) for c in candidates if c.exists()), None)


def browser_command(browser, url, screen):
    # An own profile directory: otherwise a running Edge/Chrome just opens a tab and ignores position and kiosk.
    profile = pathlib.Path(tempfile.gettempdir(), 'bitt-indicator-browser')
    cmd = [browser, f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check',
           '--disable-session-crashed-bubble', '--hide-crash-restore-bubble',
           '--disable-features=Translate,TranslateUI', '--disable-notifications',
           '--autoplay-policy=no-user-gesture-required']  # nothing may pop up: it would be projected
    if screen is not None:
        cmd += [f'--window-position={screen.x},{screen.y}', f'--window-size={screen.width},{screen.height}']
    return cmd + ['--kiosk', f'--app={url}']


def open_fullscreen(url, screen, log=print):
    """Starts the browser and returns its process (None when no Edge/Chrome was found)."""
    browser = find_browser()
    if not browser:
        log(f'no Edge or Chrome found: open {url} yourself, drag the window to the projector and press F11')
        return None
    return subprocess.Popen(browser_command(browser, url, screen), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
