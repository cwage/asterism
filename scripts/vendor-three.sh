#!/usr/bin/env bash
# Fetch the pinned three.js release from npm, check it against the
# registry's integrity hash, and copy the two files the fly-around page
# imports into static/fly/vendor/. The result is checked in, unlike the sky
# data: it is code the page runs, it is 2 MB, and a deploy should not
# depend on npm being up. This only needs running to change the version.
set -euo pipefail

VERSION=0.186.1
INTEGRITY=sha512-blFeqb49wRCSGUGj7gtpfnSGHy2lwDk94RhUmS1c/hTby70kvChbWpkJ4Pm1390LqzzvTmzgXKHPEafJwCb8jA==

cd "$(dirname "$0")/.."
dest=static/fly/vendor/three
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

curl -fsSL -o "$tmp/three.tgz" "https://registry.npmjs.org/three/-/three-$VERSION.tgz"
got="sha512-$(openssl dgst -sha512 -binary "$tmp/three.tgz" | base64 -w0)"
if [ "$got" != "$INTEGRITY" ]; then
  echo "integrity mismatch for three@$VERSION: got $got" >&2
  exit 1
fi

tar -xzf "$tmp/three.tgz" -C "$tmp" package/build/three.module.js package/build/three.core.js package/LICENSE
mkdir -p "$dest"
cp "$tmp/package/build/three.module.js" "$tmp/package/build/three.core.js" "$tmp/package/LICENSE" "$dest/"
echo "$VERSION" > "$dest/VERSION"
echo "vendored three@$VERSION into $dest"
