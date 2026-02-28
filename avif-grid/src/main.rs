//! avif-grid — assemble JPEG tiles into an AVIF grid container.
//!
//! # Protocol
//!
//! Reads a JSON object from stdin:
//! ```json
//! {
//!   "tiles": ["path/to/tile0.jpg", "path/to/tile1.jpg", ...],
//!   "quality": 55,
//!   "output": "path/to/out.avif"
//! }
//! ```
//!
//! Writes a JSON object to stdout on success:
//! ```json
//! {"cols": 32, "rows": 4, "tileSize": 256}
//! ```
//!
//! Exits non-zero and writes an error message to stderr on failure.
//!
//! # Grid layout
//!
//! Tiles are arranged in a square-ish grid: `cols = ceil(sqrt(n))`,
//! `rows = ceil(n / cols)`.  The last row is padded with blank black tiles
//! if `n` is not a multiple of `cols`.
//!
//! The caller (Whitebeard agent) is responsible for ordering tiles by photo
//! content_hash before passing them here, to ensure stable tile indices.

use std::io::{self, Read};
use std::os::raw::c_int;
use std::path::PathBuf;

use image::{DynamicImage, GenericImageView, RgbImage};
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Minimal libavif FFI — mirrors avif.h from libavif ≥ 1.0.0
//
// avifImage is fully opaque: we never access its fields, only pass pointers.
// avifEncoder is partially defined: we only set the few fields we need.
// Layout is verified against libavif 1.3.0 headers (see avif.h).
// ---------------------------------------------------------------------------

mod ffi {
    use std::os::raw::c_int;

    // avifImage — opaque C struct (heap-allocated by avifImageCreate).
    #[repr(C)]
    pub struct AvifImage {
        _opaque: [u8; 0],
    }

    // avifEncoder — partial repr(C) covering only the fields we write.
    // Fields after quality_alpha are NOT included; the heap allocation by
    // avifEncoderCreate() is always the full C struct size.
    // Offsets verified against avif.h for libavif ≥ 1.0.0:
    //   codec_choice (i32 @ 0), max_threads (i32 @ 4), speed (i32 @ 8),
    //   _kfi (i32 @ 12), timescale (u64 @ 16), _rep (i32 @ 24),
    //   _layers (u32 @ 28), quality (i32 @ 32), quality_alpha (i32 @ 36).
    #[repr(C)]
    pub struct AvifEncoder {
        pub codec_choice: c_int,   // avifCodecChoice — use 0 (AUTO)
        pub max_threads: c_int,
        pub speed: c_int,
        _keyframe_interval: c_int,
        _timescale: u64,
        _repetition_count: c_int,
        _extra_layer_count: u32,
        pub quality: c_int,
        pub quality_alpha: c_int,
    }

    // avifRGBImage — fully defined; layout from avif.h / bindgen output.
    // Alignment of pixels (*mut u8, 8 bytes) introduces 4 bytes padding after
    // max_threads (i32 @ 40) so pixels lands at offset 48.
    #[repr(C)]
    pub struct AvifRgbImage {
        pub width: u32,
        pub height: u32,
        pub depth: u32,
        pub format: u32,              // avifRGBFormat
        pub chroma_upsampling: u32,   // avifChromaUpsampling
        pub chroma_downsampling: u32, // avifChromaDownsampling
        pub avoid_libyuv: c_int,      // avifBool
        pub ignore_alpha: c_int,      // avifBool
        pub alpha_premultiplied: c_int, // avifBool
        pub is_float: c_int,          // avifBool
        pub max_threads: c_int,
        // 4 bytes implicit padding here (pointer alignment)
        pub pixels: *mut u8,
        pub row_bytes: u32,
    }

    // avifRWData — simple output buffer (data + size).
    #[repr(C)]
    pub struct AvifRwData {
        pub data: *mut u8,
        pub size: usize,
    }

    impl Default for AvifRwData {
        fn default() -> Self {
            AvifRwData { data: std::ptr::null_mut(), size: 0 }
        }
    }

    // Result codes.
    pub const AVIF_RESULT_OK: c_int = 0;

    // Pixel formats.
    pub const AVIF_PIXEL_FORMAT_YUV420: u32 = 3;

    // RGB formats.
    pub const AVIF_RGB_FORMAT_RGB: u32 = 0;

    // Chroma.
    pub const AVIF_CHROMA_UPSAMPLING_AUTOMATIC: u32 = 0;
    pub const AVIF_CHROMA_DOWNSAMPLING_AUTOMATIC: u32 = 0;

    // Add-image flags.
    pub const AVIF_ADD_IMAGE_FLAG_SINGLE: u32 = 2;

    extern "C" {
        pub fn avifImageCreate(
            width: u32,
            height: u32,
            depth: u32,
            yuv_format: u32,
        ) -> *mut AvifImage;
        pub fn avifImageDestroy(image: *mut AvifImage);

        pub fn avifImageRGBToYUV(
            image: *mut AvifImage,
            rgb: *const AvifRgbImage,
        ) -> c_int;

        pub fn avifEncoderCreate() -> *mut AvifEncoder;
        pub fn avifEncoderDestroy(encoder: *mut AvifEncoder);
        pub fn avifEncoderAddImageGrid(
            encoder: *mut AvifEncoder,
            grid_cols: u32,
            grid_rows: u32,
            cell_images: *const *const AvifImage,
            add_image_flags: u32,
        ) -> c_int;
        pub fn avifEncoderFinish(
            encoder: *mut AvifEncoder,
            output: *mut AvifRwData,
        ) -> c_int;

        pub fn avifRWDataFree(raw: *mut AvifRwData);
    }
}

use ffi::*;

// ---------------------------------------------------------------------------
// I/O types
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct Input {
    tiles: Vec<PathBuf>,
    quality: u8,
    output: PathBuf,
}

#[derive(Serialize)]
struct Output {
    cols: u32,
    rows: u32,
    #[serde(rename = "tileSize")]
    tile_size: u32,
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

fn main() {
    let mut stdin = String::new();
    io::stdin().read_to_string(&mut stdin).expect("failed to read stdin");

    let input: Input = serde_json::from_str(&stdin).expect("invalid JSON input");

    match run(input) {
        Ok(out) => println!("{}", serde_json::to_string(&out).unwrap()),
        Err(e) => {
            eprintln!("avif-grid error: {e}");
            std::process::exit(1);
        }
    }
}

// ---------------------------------------------------------------------------
// Core logic
// ---------------------------------------------------------------------------

fn run(input: Input) -> Result<Output, Box<dyn std::error::Error>> {
    let n = input.tiles.len();
    if n == 0 {
        return Err("no tiles provided".into());
    }

    // Compute grid dimensions: square-ish, minimising wasted space.
    let cols = (n as f64).sqrt().ceil() as u32;
    let rows = n.div_ceil(cols as usize) as u32;

    // Load the first tile to determine tile dimensions (all tiles must match).
    let first = load_jpeg(&input.tiles[0])?;
    let (tile_w, tile_h) = first.dimensions();
    let tile_size = tile_w.min(tile_h); // short edge

    // Build YUV420 avifImage cells — one per grid slot (padding with black).
    let total_cells = (cols * rows) as usize;

    // RAII guard to destroy avifImages on early exit.
    struct CellGuard(Vec<*mut AvifImage>);
    impl Drop for CellGuard {
        fn drop(&mut self) {
            for p in &self.0 {
                unsafe { avifImageDestroy(*p) };
            }
        }
    }
    let mut guard = CellGuard(Vec::with_capacity(total_cells));

    for i in 0..total_cells {
        let rgb = if i < n {
            load_jpeg(&input.tiles[i])?.into_rgb8()
        } else {
            // Padding: black tile of the same size.
            RgbImage::new(tile_w, tile_h)
        };

        if rgb.width() != tile_w || rgb.height() != tile_h {
            return Err(format!(
                "tile {} has size {}×{}, expected {}×{}",
                i, rgb.width(), rgb.height(), tile_w, tile_h,
            )
            .into());
        }

        let avif_img = rgb_to_yuv420(&rgb)?;
        guard.0.push(avif_img);
    }

    // Build a const-pointer array for avifEncoderAddImageGrid.
    let const_ptrs: Vec<*const AvifImage> = guard.0.iter().map(|p| *p as *const _).collect();

    // Create and configure the encoder.
    let encoder = unsafe { avifEncoderCreate() };
    if encoder.is_null() {
        return Err("avifEncoderCreate returned null".into());
    }
    struct EncoderGuard(*mut AvifEncoder);
    impl Drop for EncoderGuard {
        fn drop(&mut self) {
            unsafe { avifEncoderDestroy(self.0) };
        }
    }
    let _enc_guard = EncoderGuard(encoder);

    unsafe {
        (*encoder).quality = input.quality as c_int;
        (*encoder).quality_alpha = 100; // no alpha channel in tile cache
        (*encoder).speed = 6;           // balanced encode speed (0=slowest, 10=fastest)
        (*encoder).max_threads = 1;
    }

    // Encode the grid.
    let rc = unsafe {
        avifEncoderAddImageGrid(
            encoder,
            cols,
            rows,
            const_ptrs.as_ptr(),
            AVIF_ADD_IMAGE_FLAG_SINGLE,
        )
    };
    if rc != AVIF_RESULT_OK {
        return Err(format!("avifEncoderAddImageGrid failed (code {rc})").into());
    }

    // Finish encoding and retrieve the AVIF bitstream.
    let mut output_data = AvifRwData::default();
    let rc = unsafe { avifEncoderFinish(encoder, &mut output_data) };
    if rc != AVIF_RESULT_OK {
        return Err(format!("avifEncoderFinish failed (code {rc})").into());
    }

    let avif_bytes =
        unsafe { std::slice::from_raw_parts(output_data.data, output_data.size) }.to_vec();
    unsafe { avifRWDataFree(&mut output_data) };

    std::fs::write(&input.output, &avif_bytes)?;

    // guard and _enc_guard drop here, freeing all avifImages and the encoder.
    Ok(Output { cols, rows, tile_size })
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn load_jpeg(path: &PathBuf) -> Result<DynamicImage, Box<dyn std::error::Error>> {
    image::open(path).map_err(|e| format!("failed to open {}: {e}", path.display()).into())
}

/// Convert an RGB8 tile image to a libavif YUV420 avifImage (caller must destroy).
///
/// Points `AvifRgbImage.pixels` directly at the tile buffer (no copy) since
/// `avifImageRGBToYUV` reads synchronously and the rgb reference stays alive.
fn rgb_to_yuv420(rgb: &RgbImage) -> Result<*mut AvifImage, Box<dyn std::error::Error>> {
    let w = rgb.width();
    let h = rgb.height();

    let avif_img = unsafe { avifImageCreate(w, h, 8, AVIF_PIXEL_FORMAT_YUV420) };
    if avif_img.is_null() {
        return Err("avifImageCreate returned null".into());
    }

    let rgb_desc = AvifRgbImage {
        width: w,
        height: h,
        depth: 8,
        format: AVIF_RGB_FORMAT_RGB,
        chroma_upsampling: AVIF_CHROMA_UPSAMPLING_AUTOMATIC,
        chroma_downsampling: AVIF_CHROMA_DOWNSAMPLING_AUTOMATIC,
        avoid_libyuv: 0,
        ignore_alpha: 1,
        alpha_premultiplied: 0,
        is_float: 0,
        max_threads: 1,
        pixels: rgb.as_raw().as_ptr() as *mut u8,
        row_bytes: w * 3,
    };

    let rc = unsafe { avifImageRGBToYUV(avif_img, &rgb_desc) };
    if rc != AVIF_RESULT_OK {
        unsafe { avifImageDestroy(avif_img) };
        return Err(format!("avifImageRGBToYUV failed (code {rc})").into());
    }

    Ok(avif_img)
}
