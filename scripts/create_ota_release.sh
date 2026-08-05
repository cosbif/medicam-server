#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Usage: $0 MAJOR.MINOR.PATCH" >&2
    exit 2
fi

VERSION="$1"
TAG="medicam-v$VERSION"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIGNING_KEY="${MEDICAM_OTA_SIGNING_KEY:-$HOME/.config/medicam/ota_signing_key}"
ALLOWED_SIGNERS="$REPO_ROOT/deploy/ota_allowed_signers"

cd "$REPO_ROOT"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Refusing to release a dirty working tree" >&2
    exit 1
fi
if [[ ! -f "$SIGNING_KEY" ]]; then
    echo "OTA signing key is missing: $SIGNING_KEY" >&2
    exit 1
fi
if git show-ref --verify --quiet "refs/tags/$TAG"; then
    echo "Release tag already exists: $TAG" >&2
    exit 1
fi

git -c gpg.format=ssh \
    -c user.signingkey="$SIGNING_KEY" \
    tag -s "$TAG" -m "Medicam signed OTA release $VERSION"

git -c gpg.format=ssh \
    -c gpg.ssh.allowedSignersFile="$ALLOWED_SIGNERS" \
    verify-tag "$TAG"

git push origin "$TAG"
echo "Published signed OTA release $TAG at $(git rev-list -n 1 "$TAG")"
