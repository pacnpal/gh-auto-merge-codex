#!/usr/bin/env python3
"""Build the prherd app for the platform this runs on.

  python packaging/build.py --version 1.2.0

Writes the version into a copy of prherd.py, makes the platform's icon from
assets/prherd.png, and runs PyInstaller:

  macOS    dist/prherd.app       (Info.plist carries the version)
  Windows  dist/prherd/          (prherd.exe carries the version and icon)
  Linux    dist/prherd/          (plus prherd.desktop and prherd.png)

Signing and archiving are the release workflow's job, so this runs the same
locally and in CI. Needs: pip install -r packaging/requirements.txt
"""
import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
BUNDLE_ID = "com.pacnpal.prherd"
# Release versions, plus the PR builds' 0.0.0-pr.N+sha.
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def numeric(version):
    """The x.y.z part: Info.plist and Windows version resources take only numbers."""
    return tuple(int(n) for n in VERSION.match(version).groups())


def stamp_source(version):
    """A copy of prherd.py with __version__ set; the checkout stays untouched."""
    src = open(os.path.join(ROOT, "prherd.py"), encoding="utf-8").read()
    out, n = re.subn(r'^__version__ = ".*"$', f'__version__ = "{version}"', src, count=1, flags=re.M)
    if n != 1:
        sys.exit("can't find __version__ in prherd.py")
    path = os.path.join(BUILD, "src", "prherd.py")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    return path


def make_icon(kind):
    """assets/prherd.png -> build/prherd.icns or .ico."""
    from PIL import Image
    src = Image.open(os.path.join(ROOT, "assets", "prherd.png")).convert("RGBA")
    path = os.path.join(BUILD, f"prherd.{kind}")
    if kind == "icns":
        src.save(path, format="ICNS")  # Pillow writes every size the format holds
    else:
        src.save(path, format="ICO", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    return path


def windows_version_file(version):
    """A PyInstaller version resource, so Explorer's Details tab names the app and version."""
    a, b, c = numeric(version)
    path = os.path.join(BUILD, "version.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({a}, {b}, {c}, 0), prodvers=({a}, {b}, {c}, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'pacnpal'),
      StringStruct('FileDescription', 'prherd'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'prherd'),
      StringStruct('OriginalFilename', 'prherd.exe'),
      StringStruct('ProductName', 'prherd'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")
    return path


def pyinstaller(source, icon, extra):
    data = f"{os.path.join(ROOT, 'assets', 'prherd.png')}{os.pathsep}assets"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
                    "--name", "prherd", "--icon", icon, "--add-data", data,
                    "--distpath", DIST, "--workpath", os.path.join(BUILD, "pyinstaller"),
                    "--specpath", BUILD, *extra, source], check=True)


def finish_mac(version, build_number):
    path = os.path.join(DIST, "prherd.app", "Contents", "Info.plist")
    with open(path, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleShortVersionString": ".".join(map(str, numeric(version))),
        "CFBundleVersion": build_number,
        "CFBundleDisplayName": "prherd",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHumanReadableCopyright": "prherd",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,  # follow the system's light or dark
    })
    with open(path, "wb") as f:
        plistlib.dump(info, f)
    return os.path.join(DIST, "prherd.app")


def finish_linux():
    app = os.path.join(DIST, "prherd")
    shutil.copy(os.path.join(ROOT, "assets", "prherd.png"), app)
    shutil.copy(os.path.join(ROOT, "packaging", "prherd.desktop"), app)
    return app


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", required=True, help="like 1.2.0, 1.2.0-rc.1 or 0.0.0-pr.14+abc1234")
    p.add_argument("--build-number", default="1", help="CFBundleVersion on macOS (default 1)")
    a = p.parse_args()
    if not VERSION.match(a.version):
        sys.exit(f"bad version {a.version!r}: expected x.y.z with an optional -prerelease or +build")
    shutil.rmtree(BUILD, ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)
    source = stamp_source(a.version)
    if sys.platform == "darwin":
        pyinstaller(source, make_icon("icns"), ["--osx-bundle-identifier", BUNDLE_ID])
        out = finish_mac(a.version, a.build_number)
    elif sys.platform == "win32":
        pyinstaller(source, make_icon("ico"), ["--version-file", windows_version_file(a.version)])
        out = os.path.join(DIST, "prherd")
    else:
        pyinstaller(source, os.path.join(ROOT, "assets", "prherd.png"), [])
        out = finish_linux()
    print(f"built {out}")


if __name__ == "__main__":
    main()
