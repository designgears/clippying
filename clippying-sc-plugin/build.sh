#!/bin/bash
# Build script for the Clippying StreamController plugin.
# Builds the Rust daemon and abi3 host extension, then copies the extension into
# clippying_native/ for packaging or optional install into StreamController.
#
# Usage: ./build.sh [clean|dev|release] [--install|-i]
#   clean   - Clean build artifacts
#   dev     - Build in dev mode (debug symbols, fast compile)
#   release - Build in release mode (optimized, default)
#   --install, -i - After build, copy plugin to StreamController plugins folder
#
# Version-agnostic: the extension uses PyO3 abi3, so one .so works on Python 3.11+.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v cargo >/dev/null 2>&1; then
    echo "Error: cargo not found (install via rustup)"
    exit 1
fi

sync_version() {
    VERSION=$(awk -F'"' '/^version = / {print $2; exit}' rust-host/Cargo.toml)
    if [ -n "$VERSION" ]; then
        sed -i "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
        sed -i "s/\"version\": \".*\"/\"version\": \"$VERSION\"/" manifest.json
    fi
}
sync_version

PROFILE=""
INSTALL_PLUGIN=false
for arg in "$@"; do
    case "$arg" in
        --install|-i) INSTALL_PLUGIN=true ;;
        clean|dev|release) [ -z "$PROFILE" ] && PROFILE="$arg" ;;
        *)
            echo "Error: Unknown option '$arg'"
            echo "Usage: $0 [clean|dev|release] [--install|-i]"
            exit 1
            ;;
    esac
done
[ -z "$PROFILE" ] && PROFILE="release"

if [ "$PROFILE" = "clean" ]; then
    set -e
    echo "Cleaning build artifacts..."
    cargo clean --manifest-path rust-host/Cargo.toml
    if [ -f ../clippying-rs/Cargo.toml ]; then
        cargo clean --manifest-path ../clippying-rs/Cargo.toml
    fi
    echo "Removing compiled extension modules..."
    rm -f clippying_native/_native*.so
    echo "Clean complete!"
    exit 0
fi

if [ "$PROFILE" != "dev" ] && [ "$PROFILE" != "release" ]; then
    echo "Error: Invalid profile '$PROFILE'"
    echo "Usage: $0 [clean|dev|release] [--install|-i]"
    exit 1
fi

set -e

echo "Building Clippying plugin Rust components (abi3, Python 3.11+)..."
echo "Profile: $PROFILE"
echo ""

TARGET_NAME="_native.abi3.so"
TARGET_DIR="clippying_native"

if [ "$PROFILE" = "release" ]; then
    CARGO_FLAGS="--release"
    TARGET_SUBDIR="release"
else
    CARGO_FLAGS=""
    TARGET_SUBDIR="debug"
fi

SOURCE_LIB="rust-host/target/${TARGET_SUBDIR}/libclippying_native.so"

echo "Target: ${TARGET_DIR}/${TARGET_NAME}"
echo ""

if [ ! -f ../clippying-rs/Cargo.toml ]; then
    echo "Error: ../clippying-rs/Cargo.toml not found (required to embed daemon)"
    exit 1
fi

echo "Building embedded daemon..."
cargo build --manifest-path ../clippying-rs/Cargo.toml $CARGO_FLAGS

echo "Building abi3 host extension..."
cargo build --manifest-path rust-host/Cargo.toml $CARGO_FLAGS

if [ ! -f "$SOURCE_LIB" ]; then
    echo "Error: $SOURCE_LIB not found after build!"
    exit 1
fi

mkdir -p "$TARGET_DIR"

echo "Copying $SOURCE_LIB -> $TARGET_DIR/$TARGET_NAME"
cp "$SOURCE_LIB" "$TARGET_DIR/$TARGET_NAME"
chmod 644 "$TARGET_DIR/$TARGET_NAME"

echo "Embedded daemon payload into ${TARGET_DIR}/${TARGET_NAME}"

PLUGIN_DEST="${CLIPPYING_PLUGIN_DEST:-$HOME/.var/app/com.core447.StreamController/data/plugins/com_designgears_clippying}"
if [ "$INSTALL_PLUGIN" = true ] && [ -d "$(dirname "$PLUGIN_DEST")" ]; then
    echo "Copying plugin to StreamController..."
    rm -rf "$PLUGIN_DEST"
    mkdir -p "$PLUGIN_DEST"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a \
            --exclude='.git' \
            --exclude='__pycache__' \
            --exclude='rust-host' \
            --exclude='*.md' \
            --exclude='build.sh' \
            --exclude='.gitignore' \
            "$SCRIPT_DIR/" "$PLUGIN_DEST/"
    else
        for f in main.py manifest.json pyproject.toml requirements.txt __init__.py actions.py; do
            [ -e "$f" ] && cp "$f" "$PLUGIN_DEST/"
        done
        for d in clippying_native locales; do
            [ -d "$d" ] && cp -r "$d" "$PLUGIN_DEST/"
        done
    fi
    echo "Plugin installed at $PLUGIN_DEST"
else
    echo "Note: StreamController plugins dir not found, skipping install (path: $(dirname "$PLUGIN_DEST"))"
fi

echo ""
echo "Build complete! Extension module is at $TARGET_DIR/$TARGET_NAME"
