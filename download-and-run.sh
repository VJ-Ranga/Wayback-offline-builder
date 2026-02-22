#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="VJ-Ranga"
REPO_NAME="Wayback-offline-builder"
REF="main"
TARGET_DIR="wayback-offline-builder"

for arg in "$@"; do
  case "$arg" in
    --ref=*) REF="${arg#*=}" ;;
    --dir=*) TARGET_DIR="${arg#*=}" ;;
  esac
done

ARCHIVE_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${REF}.tar.gz"

if command -v curl >/dev/null 2>&1; then
  FETCH_CMD=(curl -fsSL "$ARCHIVE_URL")
elif command -v wget >/dev/null 2>&1; then
  FETCH_CMD=(wget -qO- "$ARCHIVE_URL")
else
  echo "Need curl or wget to continue."
  exit 1
fi

if ! command -v tar >/dev/null 2>&1; then
  echo "Need tar to extract project archive."
  exit 1
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

echo "Downloading ${REPO_NAME} (${REF})..."
"${FETCH_CMD[@]}" > "$tmp_dir/source.tar.gz"

tar -xzf "$tmp_dir/source.tar.gz" -C "$tmp_dir"
src_dir="$tmp_dir/${REPO_NAME}-${REF}"

if [ -d "$TARGET_DIR" ]; then
  echo "Target directory '$TARGET_DIR' already exists."
  echo "Use --dir=<new-path> or remove existing directory."
  exit 1
fi

mv "$src_dir" "$TARGET_DIR"

echo "Project extracted to: $TARGET_DIR"
cd "$TARGET_DIR"

chmod +x install.sh run.sh uninstall.sh update.sh
./install.sh

echo "Starting app..."
./run.sh
