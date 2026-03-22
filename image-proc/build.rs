fn main() {
    // Link against the system libavif (pre-compiled with AOM encoder).
    // macOS:   brew install libavif
    // Linux:   apt install libavif-dev
    // Windows: vcpkg install libavif:x64-windows-static
    //          (pkg-config-lite must also be on PATH: choco install pkgconfiglite)
    // This avoids building any codec from source and eliminates the nasm/cmake requirement.
    pkg_config::probe_library("libavif")
        .expect(
            "system libavif not found.\n\
             macOS:   brew install libavif\n\
             Linux:   apt install libavif-dev\n\
             Windows: vcpkg install libavif:x64-windows-static  \
                      (also: choco install pkgconfiglite)"
        );
}
