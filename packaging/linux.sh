#!/usr/bin/env bash
# Package a Linux build of prherd every common way, for this machine's architecture.
#
#   python3 packaging/build.py --version 1.2.0   # first: makes dist/prherd
#   bash packaging/linux.sh 1.2.0                # writes out/prherd-1.2.0-linux-<arch>.*
#
# The release workflow runs build.py inside python:3.13-bullseye, so the app links against
# Debian 11's glibc (2.31) and runs on Debian 11+, Ubuntu 20.04+, RHEL/Rocky/Alma 9+,
# Fedora, openSUSE and Arch. This script then runs on the Ubuntu runner itself: it needs
# ruby, rpm, zstd and network access for fpm and appimagetool, and installs what's missing.
#
# Packages, each holding the same build:
#   .tar.gz       the app folder, to unpack anywhere and run ./prherd
#   .deb          Debian, Ubuntu, Mint, Pop!_OS          (/opt/prherd, prherd on PATH)
#   .rpm          Fedora, RHEL, Rocky, Alma, openSUSE    (same layout)
#   .pkg.tar.zst  Arch, Manjaro, EndeavourOS             (same layout)
#   .AppImage     anything else: one file, chmod +x and run
set -euo pipefail

VERSION="${1:?usage: packaging/linux.sh VERSION}"
cd "$(dirname "$0")/.."
case "$(uname -m)" in
  x86_64) arch=x64 deb=amd64 rpm=x86_64 image=x86_64 ;;
  aarch64 | arm64) arch=arm64 deb=arm64 rpm=aarch64 image=aarch64 ;;
  *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
# Package managers order 1.3.0-rc.1 after 1.3.0; "~" sorts before the release for deb and rpm,
# and pacman reads a bare "rc" the same way.
pkgver="${VERSION%%+*}"
deb_rpm_ver="${pkgver/-/\~}"
pacman_ver="${pkgver//-/}"  # pacman forbids "-" in a version: 1.3.0-rc.1 -> 1.3.0rc.1

[ -x dist/prherd/prherd ] || { echo "no dist/prherd: run packaging/build.py first" >&2; exit 1; }
sudo=$([ "$(id -u)" = 0 ] || echo sudo)
if ! command -v fpm >/dev/null || ! command -v rpmbuild >/dev/null || ! command -v zstd >/dev/null || ! command -v ar >/dev/null; then
  $sudo apt-get update -qq
  $sudo apt-get install -y -qq --no-install-recommends ruby rpm zstd binutils file libarchive-tools >/dev/null
  $sudo gem install --no-document --quiet fpm -v 1.16.0
fi

mkdir -p out
name="prherd-${VERSION}-linux-${arch}"
tar -C dist -czf "out/${name}.tar.gz" prherd

# One tree for the system packages: the app in /opt, a launcher on PATH, a menu entry.
stage="build/stage"
rm -rf "$stage"
mkdir -p "$stage/opt" "$stage/usr/bin" "$stage/usr/share/applications" "$stage/usr/share/pixmaps"
cp -R dist/prherd "$stage/opt/prherd"
rm -f "$stage/opt/prherd/prherd.desktop"
ln -s /opt/prherd/prherd "$stage/usr/bin/prherd"
cp packaging/prherd.desktop "$stage/usr/share/applications/prherd.desktop"
cp assets/prherd.png "$stage/usr/share/pixmaps/prherd.png"

common=(-s dir -C "$stage" -n prherd --license MIT --maintainer "pacnpal"
        --url "https://github.com/pacnpal/prherd"
        --description "Watch GitHub repos and squash-merge the pull requests Codex has cleared")
# The build carries Tk and most of X11, but not libxcb (PyInstaller leaves it to the system) or any fonts: a
# desktop always has both, and these make a minimal install fetch them too. fontconfig brings a font with it on
# Debian and RHEL; Arch needs one asked for.
fpm "${common[@]}" -t deb -v "$deb_rpm_ver" -a "$deb" -p "out/${name}.deb" --deb-no-default-config-files \
  -d libxcb1 -d fontconfig opt usr
fpm "${common[@]}" -t rpm -v "$deb_rpm_ver" -a "$rpm" -p "out/${name}.rpm" --rpm-os linux \
  -d 'libxcb.so.1()(64bit)' -d fontconfig opt usr
fpm "${common[@]}" -t pacman -v "$pacman_ver" -a "$rpm" -p "out/${name}.pkg.tar.zst" \
  -d libxcb -d fontconfig -d ttf-font opt usr

# AppImage: the same app folder, with AppRun and the desktop entry at the top.
appdir="build/prherd.AppDir"
rm -rf "$appdir"
mkdir -p "$appdir/usr"
cp -R dist/prherd "$appdir/usr/prherd"
cp packaging/prherd.desktop "$appdir/prherd.desktop"
cp assets/prherd.png "$appdir/prherd.png"
cat > "$appdir/AppRun" <<'EOF'
#!/bin/sh
here="$(dirname "$(readlink -f "$0")")"
exec "$here/usr/prherd/prherd" "$@"
EOF
chmod +x "$appdir/AppRun"
tool="build/appimagetool-${image}.AppImage"
[ -x "$tool" ] || {
  curl -fsSL -o "$tool" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${image}.AppImage"
  chmod +x "$tool"
}
# No FUSE in a container: run the tool from its own extracted copy.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$image" "$tool" --no-appstream "$appdir" "out/${name}.AppImage" >/dev/null 2>&1 ||
  APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$image" "$tool" --no-appstream "$appdir" "out/${name}.AppImage"

ls -l out/
