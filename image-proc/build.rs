fn main() {
    // Link against the system libavif (pre-compiled with AOM encoder by Homebrew).
    // Install with: brew install libavif
    // This avoids building any codec from source and eliminates the nasm/cmake requirement.
    pkg_config::probe_library("libavif")
        .expect("system libavif not found — install with `brew install libavif`");
}
