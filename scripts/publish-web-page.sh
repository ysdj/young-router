#!/bin/bash
# Publish the browser page for one commit, so a headless host can serve it.
#
# A desktop install builds the page locally: `build-and-install-macos.sh` copies
# `rn/apps/web/dist` into `Core/web`, and Core serves it at the local port.  A
# host with no window (lv-router, a unit running `--manage-service`) cannot
# build it, so it installs the page from
# `releases/download/web/web-<commit>.tar.gz` -- named by the commit it was
# built from, because a built bundle may not be committed beside the code it
# came from.
#
# That host asks for the page of the commit it is about to run.  A commit whose
# page was never published leaves it serving the previous page -- and a page is
# code: it drives the IPC schema of the Core it is talking to.  On 2026-11-08 a
# host running 15ca916 served the page built for 3153fa3 for an hour: the
# provider list had no Qoder row, and the tab title still read `running`, so a
# fixed string could not reach the browser that needed it.  Publishing is
# therefore part of landing a change that touches the page, not a follow-up.
#
# The page is built from a *clean checkout of the named commit* and never from
# this working tree: the asset's name is the commit, so a page carrying
# uncommitted edits would be a page attributed to code it does not contain.
#
# The release holds the page of one commit, and a publish replaces it -- the
# release that is there is deleted (tag and all) before this commit's page is
# uploaded.  Publishing HEAD is therefore also how the bucket moves forward; an
# older commit's page stops being downloadable at that moment, so publish the
# commit a host is about to run rather than one it used to run.
#
# Usage:
#   scripts/publish-web-page.sh                  # HEAD
#   scripts/publish-web-page.sh --commit <sha>    # a commit already on the branch
#   scripts/publish-web-page.sh --output <dir>    # build the tarball, upload nothing
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REPOSITORY=ysdj/young-router
TAG=web
# The bucket holds the page of *one* commit, and a publish replaces it.
#
# It accumulated one asset per commit -- fourteen of them at last count -- and
# the reason is that nothing ever removed one: the publisher uploaded
# `web-<commit>.tar.gz` beside whatever was already there, so the bucket's only
# growing edge was the list of commits whose page it happened to witness.  A
# host installs exactly the page of the commit it is about to run, so a page
# for any other commit is an artifact nothing reads, kept public and permanent.
# Replacing the release is therefore the publisher's job, not a cleanup someone
# remembers: the asset that was there is deleted before the new one lands.
#
# This is also what makes the bucket a prerelease rather than a release list
# entry: GitHub shows a release on `/releases` and, unless it says otherwise,
# treats the newest one as "Latest" -- the bucket did become the repository's
# latest release, and `/releases/latest` redirected to a page tarball.  A
# prerelease is left out of that slot by GitHub's own rule.  `v<version>` is
# what this repository releases.
RELEASE_TITLE="Browser page builds (latest commit only)"
RELEASE_NOTES="The browser page Core serves at /ui, for one commit at a time: this release is replaced on every publish. Artifacts only: it is not a version of Young Router and carries no release notes."

commit_ref=HEAD
output_dir=""
while (($# > 0)); do
  case "$1" in
    --commit) commit_ref=${2:?--commit needs a revision}; shift 2 ;;
    --output) output_dir=${2:?--output needs a directory}; shift 2 ;;
    -h|--help)
      sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "publish-web-page: unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v node >/dev/null 2>&1 || {
  echo "publish-web-page: node is required to build the page" >&2
  exit 1
}
# The page build resolves React, react-native-web and esbuild out of this
# directory.  It is a dependency tree, not a source: the checkout that publishes
# is expected to have run `pnpm install` for the React Native workspace.
app_modules="$ROOT/rn/apps/web/node_modules"
if [[ ! -d "$app_modules" ]]; then
  echo "publish-web-page: rn/apps/web/node_modules is missing; run (cd rn && pnpm install)" >&2
  exit 1
fi

commit=$(git -C "$ROOT" rev-parse --verify --quiet "$commit_ref^{commit}") || {
  echo "publish-web-page: $commit_ref is not a commit in this checkout" >&2
  exit 1
}
short=${commit:0:7}

work=$(mktemp -d "${TMPDIR:-/tmp}/yr-page.XXXXXX")
tree="$work/tree"
stage="$work/stage"
cleanup() {
  git -C "$ROOT" worktree remove --force "$tree" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

# `--detach` states the commit outright: a branch name could move between this
# line and the build, and the asset is named after what was actually built.
git -C "$ROOT" worktree add --detach "$tree" "$commit" >/dev/null
# Dependencies are not part of a commit and are not cloned with it.  The build
# only ever asks for them by path, so one link is enough; nothing is written
# into the checkout.
ln -s "$app_modules" "$tree/rn/apps/web/node_modules"

node "$tree/rn/apps/web/build.mjs" >/dev/null

dist="$tree/rn/apps/web/dist"
# What a page is: one shell, one content-addressed script, the editor document
# it frames, and the shim.  `bundle.js` is refused by name -- a fixed script name
# under Core's `immutable` header is a browser frozen on whichever build it saw
# first, which is why the builder names the script after its own bytes.
bundle_name=none
for candidate in "$dist"/bundle.*.js; do
  [[ -e "$candidate" ]] || continue
  if [[ "$bundle_name" != none ]]; then
    echo "publish-web-page: the build wrote more than one bundle script" >&2
    exit 1
  fi
  bundle_name=$(basename "$candidate")
done
[[ -n "$bundle_name" ]] || { echo "publish-web-page: the build wrote no content-addressed bundle" >&2; exit 1; }
[[ "$bundle_name" =~ ^bundle\.[0-9a-f]{16}\.js$ ]] || { echo "publish-web-page: $bundle_name is not bundle.<16 hex>.js" >&2; exit 1; }
[[ ! -e "$dist/bundle.js" ]] || { echo "publish-web-page: the build left a fixed bundle.js" >&2; exit 1; }
# The editor is a script of its own, addressed by content.  The framed page is
# then a few KB instead of half a megabyte of inlined Ace, which a page that is
# answered `no-store` made every open pay for again.
editor_name=none
for candidate in "$dist"/editor.*.js; do
  [[ -e "$candidate" ]] || continue
  if [[ "$editor_name" != none ]]; then
    echo "publish-web-page: the build wrote more than one editor script" >&2
    exit 1
  fi
  editor_name=$(basename "$candidate")
done
[[ "$editor_name" =~ ^editor\.[0-9a-f]{16}\.js$ ]] || { echo "publish-web-page: $editor_name is not editor.<16 hex>.js" >&2; exit 1; }
for name in index.html shim.js editor.html "$bundle_name" "$editor_name"; do
  [[ -s "$dist/$name" ]] || { echo "publish-web-page: the build has no $name" >&2; exit 1; }
done
# Every text asset travels with both precompressed siblings, which is what Core
# answers from: a missing one costs the browser the whole file, over a link that
# may be the reason this page is published at all.
for name in index.html editor.html "$bundle_name" "$editor_name"; do
  for codec in br gz; do
    [[ -s "$dist/$name.$codec" ]] || { echo "publish-web-page: the build has no $name.$codec" >&2; exit 1; }
  done
done
# The shell is the only thing that names the script.  A shell pointing at a name
# the build did not write renders blank -- the failure the host's installer must
# never accept.
grep -q "src=\"\./$bundle_name\"" "$dist/index.html" || {
  echo "publish-web-page: index.html does not load $bundle_name" >&2
  exit 1
}
grep -q "src=\"\./$editor_name\"" "$dist/editor.html" || {
  echo "publish-web-page: editor.html does not load $editor_name" >&2
  exit 1
}

mkdir -p "$stage/web-$short"
cp -a "$dist/." "$stage/web-$short/"
# `built_from` is what the host compares before it restarts onto a commit.  The
# digests are the identity of the bytes actually shipped: the same commit must
# publish the same page, and a rebuild that disagrees is a reproduction failure
# rather than an update.
node - "$stage/web-$short" "$commit" <<'NODE'
const { createHash } = require("node:crypto");
const { readdirSync, readFileSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");
const directory = process.argv[2];
const files = {};
for (const name of readdirSync(directory).sort()) {
  // The transport encodings are derived from these bytes, not part of them.
  // Brotli's own output may differ between library versions, and a page
  // identified by its encoding would read as a different page for a reason
  // nobody shipped.  Core serves `x.br` only for a request for `x`, and `x` is
  // named after its own digest, so a sibling cannot go stale either.
  if (name.endsWith(".br") || name.endsWith(".gz")) continue;
  files[name] = createHash("sha256").update(readFileSync(join(directory, name))).digest("hex");
}
writeFileSync(
  join(directory, "MANIFEST.json"),
  `${JSON.stringify({ built_from: process.argv[3], files }, null, 2)}\n`,
);
NODE

asset="web-$commit.tar.gz"
# Archive metadata is not part of the page.  A build's own mtimes differ every
# run, so they are replaced with one fixed stamp: what the asset holds, and what
# a rebuild must reproduce, is the content.
find "$stage" -exec touch -h -t 200001010000 {} +
tar -czf "$work/$asset" -C "$stage" "web-$short"
if command -v shasum >/dev/null 2>&1; then
  digest=$(shasum -a 256 "$stage/web-$short/MANIFEST.json" | cut -d' ' -f1)
  bytes=$(shasum -a 256 "$work/$asset" | cut -d' ' -f1)
else
  digest=$(sha256sum "$stage/web-$short/MANIFEST.json" | cut -d' ' -f1)
  bytes=$(sha256sum "$work/$asset" | cut -d' ' -f1)
fi

if [[ -n "$output_dir" ]]; then
  mkdir -p "$output_dir"
  mv "$work/$asset" "$output_dir/$asset"
  echo "$output_dir/$asset"
  echo "page $digest"
  echo "archive $bytes"
  exit 0
fi

command -v gh >/dev/null 2>&1 || {
  echo "publish-web-page: gh is required to upload $asset (pass --output to build only)" >&2
  exit 1
}
# The bucket holds one asset: the page of one commit.  Whatever it holds is
# replaced, not added to -- a second `web-<commit>.tar.gz` is a page no host will
# install (each host asks for its own commit), so leaving it is a public
# artifact with no reader.
#
# The archive is built and verified above, before anything remote is touched, so
# the only exposure is the two calls that swap the release.  A failure inside
# that window leaves the bucket empty, and that is the failure the host is
# written to survive: `lv-web-bundle --from-release` exits non-zero when the
# asset is not there, and `yr-sync` answers by keeping the page already
# installed ("keeping the installed page: none published for <commit>") -- a
# stopped release is a host on its previous page, not a host with no page.
#
# Each run therefore does the whole cycle: delete the release that is there
# (tag included), create it again as a prerelease, upload this commit's page.
# Republishing a commit rebuilds and replaces its own page byte for byte -- the
# build is reproducible, so what a host downloads for `<commit>` is still that
# commit's page.
if gh release view "$TAG" --repo "$REPOSITORY" >/dev/null 2>&1; then
  previous=$(gh release view "$TAG" --repo "$REPOSITORY" --json assets \
    --jq '.assets[].name' | tr '\n' ' ')
  echo "replacing release $TAG (${previous:-no assets})" >&2
  gh release delete "$TAG" --repo "$REPOSITORY" --cleanup-tag --yes >/dev/null
fi
gh release create "$TAG" --repo "$REPOSITORY" --title "$RELEASE_TITLE" \
  --prerelease --latest=false --notes "$RELEASE_NOTES" --target "$commit" \
  >/dev/null

gh release upload "$TAG" "$work/$asset" --repo "$REPOSITORY"
echo "published: $asset (page $digest, archive $bytes) -- release $TAG replaced"

