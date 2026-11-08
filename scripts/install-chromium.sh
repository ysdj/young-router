#!/usr/bin/env bash
#
# Install the one thing a station sign-in needs on a host with no browser.
#
# Core drives a Chromium-family browser to sign in to a relay station: over CDP
# when the machine has a screen, and headless with its frames streamed to the
# page when it does not.  Nothing else is required — no X server, no VNC — but a
# server image often ships without any browser at all, and then the sign-in
# answers `no_browser` and only the password form is left.
#
# This states which browser Core will drive and installs one when there is none.
# It is idempotent: a host that already has Chrome, Chromium, Edge or Brave is
# told which one and nothing is downloaded.
#
#   scripts/install-chromium.sh            install when missing
#   scripts/install-chromium.sh --check    report only; exit 1 when missing
#
# What it deliberately does not do: download a browser tarball of its own.  The
# browser must be the distribution's (or the vendor's) package so its security
# updates keep arriving; a private copy would be one nobody patches.
#
# It also installs the CJK font a station's own pages are written in. Measured on
# a host with Chrome and no CJK font: every Chinese label on the station's login
# page came back as a box, so 用户名 and 密码 were indistinguishable. macOS ships
# those fonts, so this is a Linux-only step.
#
# Exit codes: 0 installed or present, 1 missing (--check), 2 unsupported host,
# 3 present but not installable without an operator (no root, no sudo -n).
set -euo pipefail

check_only=false
while (($# > 0)); do
  case "$1" in
    --check) check_only=true; shift ;;
    -h|--help)
      sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "install-chromium: unknown argument: $1" >&2; exit 2 ;;
  esac
done

# The same order Core uses, so "which browser will it drive" has one answer.
# YOUNG_ROUTER_BROWSER states it instead; that is reported as present without
# going looking for anything.
# The bundle a name means on macOS, so `chromium` resolves the way Core's own
# resolver does instead of only on PATH.
BUNDLES="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
/Applications/Chromium.app/Contents/MacOS/Chromium
/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge
/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

by_name() {
  local wanted=$1 name path bundle
  for name in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge brave-browser; do
    if [ "$(printf '%s' "$wanted" | tr '[:upper:]' '[:lower:]')" = "$name" ]; then
      path=$(command -v "$name" 2>/dev/null || true)
      if [ -n "$path" ]; then
        echo "$path"
        return 0
      fi
    fi
  done
  while IFS= read -r bundle; do
    if [ "$(basename "$bundle" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$wanted" | tr '[:upper:]' '[:lower:]')" ] && [ -x "$bundle" ]; then
      echo "$bundle"
      return 0
    fi
  done <<< "$BUNDLES"
  path=$(command -v "$wanted" 2>/dev/null || true)
  if [ -n "$path" ]; then
    echo "$path"
    return 0
  fi
  return 1
}

existing() {
  local stated=${YOUNG_ROUTER_BROWSER:-} path
  if [ -n "$stated" ]; then
    if [ -x "$stated" ]; then
      echo "$stated"
      return 0
    fi
    by_name "$stated"
    return $?
  fi
  local name
  for name in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge brave-browser; do
    path=$(command -v "$name" 2>/dev/null || true)
    if [ -n "$path" ]; then
      echo "$path"
      return 0
    fi
  done
  while IFS= read -r path; do
    if [ -x "$path" ]; then
      echo "$path"
      return 0
    fi
  done <<< "$BUNDLES"
  return 1
}

# fontconfig's own answer, not a package name that may be installed and unusable.
fonts_present() {
  # Only a Linux host is ours to font-manage: macOS and Windows ship the CJK
  # faces a station's page needs, and have no `fc-list` to ask.
  command -v apt-get >/dev/null 2>&1 || return 0
  command -v fc-list >/dev/null 2>&1 || return 1
  fc-list :lang=zh 2>/dev/null | head -1 | grep -q .
}

install_fonts() {
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fontconfig fonts-noto-cjk >/dev/null || return $?
  as_root fc-cache -f >/dev/null 2>&1 || true
  return 0
}

as_root() {
  if [ "$(id -u)" = "0" ]; then
    "$@"
  elif sudo -n true 2>/dev/null; then
    sudo -n "$@"
  else
    return 3
  fi
}

install_browser() {
  if command -v apt-get >/dev/null 2>&1; then
    # Debian publishes `chromium`; Ubuntu's `chromium-browser` is a snap
    # wrapper, which a server image without snapd cannot run.  Try the real
    # package first and say so rather than half-installing snap.
    as_root apt-get update -qq || return $?
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends chromium || return $?
  elif command -v dnf >/dev/null 2>&1; then
    as_root dnf install -y chromium || return $?
  elif command -v zypper >/dev/null 2>&1; then
    as_root zypper --non-interactive install chromium || return $?
  elif command -v pacman >/dev/null 2>&1; then
    as_root pacman -Sy --noconfirm chromium || return $?
  elif command -v apk >/dev/null 2>&1; then
    as_root apk add --no-cache chromium || return $?
  elif command -v brew >/dev/null 2>&1; then
    # Homebrew's `chromium` cask is disabled (it fails Gatekeeper), so the
    # vendor build is the one that works unattended.
    brew install --cask google-chrome || return $?
  else
    return 2
  fi
}

# `set -e` would otherwise make a missing browser look like a typo in this file.
found=""
existing >/dev/null 2>&1 && found=$(existing)
fonts_ok=0
fonts_present && fonts_ok=1

if [ -n "$found" ] && [ "$fonts_ok" = 1 ]; then
  echo "install-chromium: using $found (CJK fonts present)"
  exit 0
fi

if [ "$check_only" = true ]; then
  [ -n "$found" ] || echo "install-chromium: no Chromium-family browser on this host" >&2
  [ "$fonts_ok" = 1 ] || echo "install-chromium: no CJK font; a station's Chinese labels would render as boxes" >&2
  exit 1
fi
if [ -n "$found" ]; then
  echo "install-chromium: using $found"
fi

if [ -z "$found" ]; then
  if ! install_browser; then
    status=$?
    if [ "$status" = "2" ]; then
      echo "install-chromium: no package manager here knows chromium; install Google Chrome, Chromium, Edge or Brave by hand" >&2
      exit 2
    fi
    echo "install-chromium: could not install a browser without an operator (no root and no passwordless sudo)" >&2
    echo "install-chromium: run 'apt-get install -y chromium' (or the equivalent) on this host" >&2
    exit 3
  fi
  existing >/dev/null 2>&1 && found=$(existing)
  if [ -z "$found" ]; then
    echo "install-chromium: the package manager finished and no browser is on PATH" >&2
    exit 2
  fi
  echo "install-chromium: installed $found"
fi

if [ "$fonts_ok" != 1 ]; then
  if ! install_fonts; then
    echo "install-chromium: the CJK font package could not be installed; Chinese labels will render as boxes" >&2
  else
    echo "install-chromium: installed fonts-noto-cjk"
  fi
fi
echo "install-chromium: using $found"
exit 0
