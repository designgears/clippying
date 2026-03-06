#!/bin/bash
# Build clippying plugin Rust components and copy them into plugin package.
# Usage: ./build.sh [clean|dev|release] [--install|-i]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v cargo >/dev/null 2>&1; then
    echo "Error: cargo not found (install via rustup)"
    exit 1
fi

PROFILE="release"
INSTALL_PLUGIN=false
for arg in "$@"; do
    case "$arg" in
        clean|dev|release) PROFILE="$arg" ;;
        --install|-i) INSTALL_PLUGIN=true ;;
        *) echo "Error: Unknown option '$arg'"; echo "Usage: $0 [clean|dev|release] [--install|-i]"; exit 1 ;;
    esac
done

if [ "$PROFILE" = "clean" ]; then
    set -e
    cargo clean --manifest-path rust-host/Cargo.toml
    if [ -f ../clippying-rs/Cargo.toml ]; then
        cargo clean --manifest-path ../clippying-rs/Cargo.toml
    fi
    rm -f clippying_native/_native*.so
    echo "Clean complete"
    exit 0
fi

if [ "$PROFILE" = "release" ]; then
    CARGO_FLAGS="--release"
    TARGET_SUBDIR="release"
else
    CARGO_FLAGS=""
    TARGET_SUBDIR="debug"
fi

set -e

# Build daemon first so rust-host can embed it in _native.abi3.so via build.rs
if [ -f ../clippying-rs/Cargo.toml ]; then
    cargo build --manifest-path ../clippying-rs/Cargo.toml $CARGO_FLAGS
else
    echo "Error: ../clippying-rs/Cargo.toml not found (required to embed daemon)"
    exit 1
fi

cargo build --manifest-path rust-host/Cargo.toml $CARGO_FLAGS

SOURCE_LIB="rust-host/target/${TARGET_SUBDIR}/libclippying_native.so"
TARGET_LIB="clippying_native/_native.abi3.so"

if [ ! -f "$SOURCE_LIB" ]; then
    echo "Error: $SOURCE_LIB not found after build"
    exit 1
fi

cp "$SOURCE_LIB" "$TARGET_LIB"
chmod 644 "$TARGET_LIB"

echo "Built extension: $TARGET_LIB"
echo "Embedded daemon payload into _native.abi3.so"

PLUGIN_DEST="${CLIPPYING_PLUGIN_DEST:-$HOME/.var/app/com.core447.StreamController/data/plugins/com_designgears_clippying}"
if [ "$INSTALL_PLUGIN" = true ] && [ -d "$(dirname "$PLUGIN_DEST")" ]; then
    rm -rf "$PLUGIN_DEST"
    mkdir -p "$PLUGIN_DEST"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a \
            --exclude='.git' \
            --exclude='__pycache__' \
            --exclude='rust-host/target' \
            --exclude='*.md' \
            --exclude='build.sh' \
            "$SCRIPT_DIR/" "$PLUGIN_DEST/"
    else
        cp -r "$SCRIPT_DIR/"* "$PLUGIN_DEST/"
    fi
    echo "Plugin installed at $PLUGIN_DEST"
fi
