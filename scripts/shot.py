#!/usr/bin/env python3
"""Developer tool (macOS): screenshot the real prherd window, fed demo data by scripts/demo-gh.

  scripts/shot.py out.png [--appearance light|dark] [--size 1180x720] [--wait 5]
                          [--repos demo|none] [--top] [--do 'python'] ...

Runs prherd in this process with a throwaway settings file and the stand-in gh first on
PATH, so it never touches your settings or GitHub. After --wait seconds it runs each
--do snippet, then captures the window with `screencapture` and exits.

The capture is of the window itself, so it doesn't matter what else is on screen.

A --do snippet is Python run on the Tk thread with these names: root, tk, ttk, app
(root.app if the window sets it), tree (the first Treeview), widgets() (every widget
under root), button(text) (a ttk.Button by its label), pump(seconds) (run the event
loop for a while). --top captures the newest Toplevel (a sheet or the settings
window) instead of the main window.

CI uses scripts/screenshots.sh (Xvfb) for the README shots; this is for checking work
by eye on a Mac.
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_REPOS = ["github.com/octo-org/storefront", "github.com/octo-org/api", "github.com/octo-org/docs"]


def window_number(title):
    """This process's visible window with that title, as screencapture -l wants it. Capturing by window, not by
    screen region, keeps other apps' windows and system prompts out of the picture."""
    import ctypes
    import ctypes.util
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def msg(obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
        send = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(("objc_msgSend", objc))
        return send(obj, objc.sel_registerName(sel.encode()), *args)

    windows = msg(msg(objc.objc_getClass(b"NSApplication"), "sharedApplication"), "windows")
    found = None
    for i in range(msg(windows, "count", restype=ctypes.c_ulong)):
        window = msg(windows, "objectAtIndex:", i, argtypes=(ctypes.c_ulong,))
        name = (msg(msg(window, "title"), "UTF8String", restype=ctypes.c_char_p) or b"").decode()
        if name == title and msg(window, "isVisible", restype=ctypes.c_bool):
            found = msg(window, "windowNumber", restype=ctypes.c_long)
    if found is None:
        sys.exit(f"no window titled {title!r}")
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("out")
    p.add_argument("--appearance", choices=("light", "dark"), default="light")
    p.add_argument("--size", default="1180x720")
    p.add_argument("--wait", type=float, default=5)
    p.add_argument("--repos", choices=("demo", "none"), default="demo")
    p.add_argument("--top", action="store_true", help="capture the newest Toplevel, not the main window")
    p.add_argument("--do", action="append", default=[], help="Python to run on the Tk thread before the capture")
    p.add_argument("--settings", default="{}", help="JSON merged into the throwaway settings file")
    a = p.parse_args()

    lock = open(os.path.join(tempfile.gettempdir(), "prherd-shot.lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # one window on screen at a time, so captures don't overlap

    work = tempfile.mkdtemp(prefix="prherd-shot-")
    os.environ["XDG_CONFIG_HOME"] = work
    os.environ["DEMO_GH_LOG"] = os.path.join(work, "gh.log")
    os.environ["PATH"] = os.path.join(HERE, "demo-gh") + os.pathsep + os.environ["PATH"]
    settings = {"repos": DEMO_REPOS if a.repos == "demo" else [], "mode": "", "interval": 3600, "merging": False,
                "geometry": f"{a.size}+60+60", "appearance": a.appearance}
    settings.update(json.loads(a.settings))
    with open(os.path.join(work, "prherd.json"), "w") as f:
        json.dump(settings, f)

    sys.path.insert(0, os.path.dirname(HERE))
    import tkinter as tk
    from tkinter import ttk
    import prherd  # after the environment: it reads PATH and XDG_CONFIG_HOME at import

    def capture(root):
        def widgets(parent=root):
            yield parent
            for child in parent.winfo_children():
                yield from widgets(child)

        def pump(seconds):
            end = time.time() + seconds
            while time.time() < end:
                root.update()
                time.sleep(0.01)

        names = {"root": root, "tk": tk, "ttk": ttk, "app": getattr(root, "app", None), "widgets": widgets,
                 "pump": pump, "prherd": prherd,
                 "tree": next((w for w in widgets() if isinstance(w, ttk.Treeview)), None),
                 "button": lambda text: next(w for w in widgets() if isinstance(w, ttk.Button) and w.cget("text") == text)}
        for code in a.do:
            exec(code, names)
            pump(0.4)
        target = root
        if a.top:
            tops = [w for w in widgets() if isinstance(w, tk.Toplevel) and w.winfo_viewable()]
            if not tops:
                sys.exit("--top: no Toplevel is showing")
            target = tops[-1]
        pump(0.5)
        subprocess.run(["screencapture", "-x", "-o", f"-l{window_number(target.title())}", a.out], check=True)
        subprocess.run(["sips", "-Z", "1400", a.out], capture_output=True)  # retina captures are 2x; keep them readable
        print(f"wrote {a.out}", flush=True)
        os._exit(0)

    class Tk(tk.Tk):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.after(int(a.wait * 1000), lambda: capture(self))

    tk.Tk = Tk
    prherd.main([])


if __name__ == "__main__":
    main()
