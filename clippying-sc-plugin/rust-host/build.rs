use std::env;
use std::path::PathBuf;

fn main() {
    let manifest_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let profile = env::var("PROFILE").unwrap_or_else(|_| "release".to_string());

    let bin_path = manifest_dir
        .join("../../clippying-rs/target")
        .join(&profile)
        .join("clippying");

    println!("cargo:rerun-if-changed={}", manifest_dir.join("../../clippying-rs/src").display());
    println!("cargo:rerun-if-changed={}", bin_path.display());

    if !bin_path.exists() {
        panic!(
            "embedded daemon binary not found at {}. Build clippying-rs first for profile '{}'",
            bin_path.display(),
            profile
        );
    }

    println!("cargo:rustc-env=CLIPPYING_EMBED_BIN={}", bin_path.display());
}
