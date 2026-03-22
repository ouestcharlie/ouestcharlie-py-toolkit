fn main() {
    // Link against the system libavif (pre-compiled with AOM encoder).
    // macOS:   brew install libavif
    // Linux:   apt install libavif-dev
    // Windows: vcpkg install libavif:x64-windows-static-md  (VCPKG_ROOT must be set)
    // This avoids building any codec from source and eliminates the nasm/cmake requirement.
    if cfg!(target_os = "windows") {
        #[cfg(target_os = "windows")]
        vcpkg::probe_package("libavif")
            .expect(
                "libavif not found via vcpkg.\n\
                 Run: vcpkg install libavif:x64-windows-static-md\n\
                 and ensure VCPKG_ROOT points to your vcpkg installation."
            );
    } else {
        pkg_config::probe_library("libavif")
            .expect(
                "system libavif not found.\n\
                 macOS: brew install libavif\n\
                 Linux: apt install libavif-dev"
            );
    }
}
