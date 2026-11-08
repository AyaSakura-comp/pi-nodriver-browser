"""macOS stand-in for the Xvfb display: keep the worker's headful Chrome off the user's screen.

Linux runs Chrome inside Xvfb, an invisible X server. macOS has no equivalent, so the worker
starts the same headful Chrome and hides that one application (NSRunningApplication.hide on
its PID). Only the worker's Chrome process is hidden: the user's own Chrome is a different
process and is never touched. Hiding needs no Accessibility/Automation permission.

A hidden app's pages report document.visibilityState == "hidden" and pause
requestAnimationFrame, which Xvfb never does. Every page therefore gets CDP focus emulation
(see Worker.keep_page_visible), which makes it render as a visible, focused page again.

All calls are best effort and return False/None instead of raising: a visible Chrome window is
an inconvenience, never a reason to fail a browser command.
"""
import ctypes
import ctypes.util
import os
import re
import subprocess
import sys
import threading
import time

IS_MACOS = sys.platform == 'darwin'

_objc = None
_send_address = None
_prototypes = {}
_lock = threading.Lock()


def _runtime():
    global _objc, _send_address
    with _lock:
        if _objc is None:
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('objc') or '/usr/lib/libobjc.A.dylib')
            ctypes.cdll.LoadLibrary('/System/Library/Frameworks/AppKit.framework/AppKit')
            objc.objc_getClass.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
            objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
            _send_address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
            _objc = objc
    return _objc


def _msg(receiver, selector, *args, restype=ctypes.c_void_p, argtypes=()):
    """objc_msgSend through a per-signature prototype: setting restype/argtypes on the shared
    function object would race between the keep-hidden thread and the worker thread."""
    objc = _runtime()
    key = (restype, tuple(argtypes))
    prototype = _prototypes.get(key)
    if prototype is None:
        prototype = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(_send_address)
        _prototypes[key] = prototype
    return prototype(receiver, objc.sel_registerName(selector.encode()), *args)


class _AutoreleasePool:
    """Objective-C objects returned to a thread without a run loop are freed only by a pool."""
    def __enter__(self):
        self.token = _runtime().objc_autoreleasePoolPush()

    def __exit__(self, *exc):
        _runtime().objc_autoreleasePoolPop(self.token)


def _application(pid):
    """NSRunningApplication for a PID, or None (not a GUI app / exited / not macOS)."""
    if not IS_MACOS or not pid:
        return None
    try:
        cls = _runtime().objc_getClass(b'NSRunningApplication')
        app = _msg(cls, 'runningApplicationWithProcessIdentifier:', int(pid), argtypes=(ctypes.c_int,))
        return app or None
    except Exception:
        return None


def is_hidden(pid) -> bool | None:
    app = _application(pid)
    if app is None:
        return None
    try:
        return bool(_msg(app, 'isHidden', restype=ctypes.c_bool))
    except Exception:
        return None


def hide(pid) -> bool:
    """Hide one application (all of its windows). Returns whether the request was accepted.

    Always sends the request: the worker has no AppKit run loop, so NSRunningApplication's
    cached isHidden goes stale, and hiding an already hidden app is a no-op anyway."""
    app = _application(pid)
    if app is None:
        return False
    try:
        return bool(_msg(app, 'hide', restype=ctypes.c_bool))
    except Exception:
        return False


def lsappinfo_front_pid() -> int | None:
    """PID of the active application, live from LaunchServices. NSWorkspace's
    frontmostApplication is only refreshed by a run loop, which the worker does not have."""
    try:
        asn = subprocess.run(['lsappinfo', 'front'], capture_output=True, text=True, timeout=2).stdout.strip()
        if not asn:
            return None
        info = subprocess.run(['lsappinfo', 'info', '-only', 'pid', asn],
                              capture_output=True, text=True, timeout=2).stdout
        match = re.search(r'pid\s*=\s*(\d+)', info)
        return int(match.group(1)) if match else None
    except Exception:
        return None


def keep_hidden(pid, interval: float = 0.4) -> threading.Thread | None:
    """Safety net for anything that still unhides the worker Chrome (popups and new windows
    activate the app on macOS): re-hide it, and hand focus back to the application the user
    was using, so keystrokes never go to an invisible Chrome.

    A daemon thread, independent of the worker's asyncio loop, so a busy or blocked
    command cannot leave Chrome in front of the user. Ends when the process exits."""
    if not should_hide() or not pid:
        return None

    def run():
        last_user_app = None
        while True:
            try:
                os.kill(int(pid), 0)
            except OSError:
                return
            try:
                front = lsappinfo_front_pid()
                if front and front != pid:
                    last_user_app = front
                with _AutoreleasePool():
                    hide(pid)
                    if front == pid and last_user_app:
                        activate(last_user_app)
            except Exception:
                pass
            time.sleep(interval)

    thread = threading.Thread(target=run, name=f'macos-keep-hidden-{pid}', daemon=True)
    thread.start()
    return thread


def frontmost_pid() -> int | None:
    if not IS_MACOS:
        return None
    try:
        workspace = _msg(_runtime().objc_getClass(b'NSWorkspace'), 'sharedWorkspace')
        app = _msg(workspace, 'frontmostApplication')
        if not app:
            return None
        return int(_msg(app, 'processIdentifier', restype=ctypes.c_int))
    except Exception:
        return None


def activate(pid) -> bool:
    """Give focus back to an application (the one that was in front before Chrome started)."""
    app = _application(pid)
    if app is None:
        return False
    try:
        # NSApplicationActivateAllWindows = 1 << 0
        return bool(_msg(app, 'activateWithOptions:', 1, restype=ctypes.c_bool, argtypes=(ctypes.c_ulong,)))
    except Exception:
        return False


def hide_mode() -> str:
    """PI_NODRIVER_MACOS_WINDOW: "hidden" (default) hides the worker's Chrome; "visible" shows it."""
    mode = os.environ.get('PI_NODRIVER_MACOS_WINDOW', 'hidden').strip().lower()
    return mode if mode in {'hidden', 'visible'} else 'hidden'


def should_hide() -> bool:
    return IS_MACOS and hide_mode() == 'hidden'


# --- A separate Chrome identity for the worker -------------------------------------------
#
# Running the worker with the user's own /Applications/Google Chrome.app makes two instances of
# com.google.Chrome. LaunchServices then routes links opened from other apps (`open URL`, Mail,
# Slack...) to either instance, including the hidden worker one, where they silently vanish.
# The worker therefore runs a clone of Chrome with its own bundle id and no URL/document
# handlers, so macOS never routes anything to it. Same binaries, so the browser still reports
# itself as Google Chrome. The clone is ad-hoc signed; with --use-mock-keychain (worker.py) it
# never asks for the user's "Chrome Safe Storage" keychain item.

AGENT_BUNDLE_ID = 'com.pi.nodriver.chrome'
AGENT_APP_NAME = 'Pi Agent Chrome'


def agent_chrome_dir():
    root = os.environ.get('PI_AGENT_DIR') or os.path.join(os.path.expanduser('~'), '.pi', 'agent')
    return os.path.join(root, 'nodriver-chrome')


def _bundle_of(executable):
    """'/X/Google Chrome.app/Contents/MacOS/Google Chrome' -> '/X/Google Chrome.app'."""
    parts = os.path.abspath(executable).split(os.sep)
    for index in range(len(parts) - 1, 0, -1):
        if parts[index].endswith('.app'):
            return os.sep.join(parts[:index + 1])
    return None


def _plist(path, *commands):
    for command in commands:
        subprocess.run(['/usr/libexec/PlistBuddy', '-c', command, path],
                       capture_output=True, text=True, timeout=10)


def _plist_value(path, key):
    result = subprocess.run(['/usr/libexec/PlistBuddy', '-c', f'Print :{key}', path],
                            capture_output=True, text=True, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else None


def agent_chrome(source_executable):
    """Executable of the worker's own Chrome clone, (re)built from source_executable when it is
    missing or the source Chrome was updated. Returns None when it cannot be built."""
    source_app = _bundle_of(source_executable)
    if not IS_MACOS or source_app is None:
        return None
    relative = os.path.relpath(source_executable, source_app)
    target_app = os.path.join(agent_chrome_dir(), f'{AGENT_APP_NAME}.app')
    target_exe = os.path.join(target_app, relative)
    source_plist = os.path.join(source_app, 'Contents', 'Info.plist')
    target_plist = os.path.join(target_app, 'Contents', 'Info.plist')
    version = _plist_value(source_plist, 'CFBundleShortVersionString')
    if (os.access(target_exe, os.X_OK) and version is not None
            and _plist_value(target_plist, 'CFBundleShortVersionString') == version
            and _plist_value(target_plist, 'CFBundleIdentifier') == AGENT_BUNDLE_ID):
        return target_exe
    try:
        os.makedirs(agent_chrome_dir(), exist_ok=True)
        staging = os.path.join(agent_chrome_dir(), f'.build-{os.getpid()}.app')
        subprocess.run(['rm', '-rf', staging], check=True, timeout=120)
        # APFS clone: no extra disk space; plain copy elsewhere.
        if subprocess.run(['cp', '-cR', source_app, staging], capture_output=True, timeout=600).returncode:
            subprocess.run(['cp', '-R', source_app, staging], check=True, capture_output=True, timeout=600)
        subprocess.run(['xattr', '-cr', staging], capture_output=True, timeout=120)
        plist = os.path.join(staging, 'Contents', 'Info.plist')
        _plist(plist, f'Set :CFBundleIdentifier {AGENT_BUNDLE_ID}', f'Set :CFBundleName {AGENT_APP_NAME}',
               'Delete :CFBundleURLTypes', 'Delete :CFBundleDocumentTypes', 'Delete :KSProductID')
        _plist(plist, f'Add :CFBundleDisplayName string {AGENT_APP_NAME}',
               f'Set :CFBundleDisplayName {AGENT_APP_NAME}')
        # (LSUIElement would drop the Dock icon, but Chrome then fails to find its framework.)
        subprocess.run(['codesign', '--force', '--deep', '--sign', '-', staging],
                       check=True, capture_output=True, timeout=600)
        old = target_app + f'.old-{os.getpid()}'
        if os.path.exists(target_app):
            os.rename(target_app, old)
        os.rename(staging, target_app)
        subprocess.run(['rm', '-rf', old], capture_output=True, timeout=120)
        # The first launch of a newly signed binary is assessed by the system; do it now, not
        # inside nodriver's short DevTools connect window.
        subprocess.run([target_exe, '--version'], capture_output=True, timeout=120)
        return target_exe
    except Exception:
        subprocess.run(['rm', '-rf', os.path.join(agent_chrome_dir(), f'.build-{os.getpid()}.app')],
                       capture_output=True)
        return target_exe if os.access(target_exe, os.X_OK) else None
