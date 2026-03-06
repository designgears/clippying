.PHONY: release debug clean

release:
	cargo build --release
	@if command -v upx >/dev/null 2>&1; then \
		upx --best --lzma target/release/clippying; \
	fi

debug:
	cargo build

clean:
	cargo clean
