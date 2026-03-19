//! avif-grid — decode photos and assemble them into an AVIF grid container.
//!
//! # Protocol
//!
//! Reads a JSON object from stdin:
//! ```json
//! {
//!   "photos": [
//!     { "path": "/tmp/staged.jpg", "ext": ".jpg", "orientation": 6, "content_hash": "sha256:..." },
//!     ...
//!   ],
//!   "tile_size": 256,
//!   "fit": "crop",
//!   "quality": 55,
//!   "output": "/tmp/output.avif"
//! }
//! ```
//! `orientation` is optional (`null` → no transform applied).
//! `fit` is `"crop"` (center-crop to square) or `"pad"` (letterbox with black).
//!
//! Writes a JSON object to stdout on success:
//! ```json
//! {"cols": 32, "rows": 4, "tileSize": 256, "photoOrder": ["sha256:aaa...", ...]}
//! ```
//! `photoOrder` reflects the tile order passed in (caller sorts by content_hash).
//!
//! Exits non-zero and writes an error message to stderr on failure.
//!
//! # Grid layout
//!
//! Photos are arranged in a square-ish grid: `cols = ceil(sqrt(n))`,
//! `rows = ceil(n / cols)`.  The last row is padded with blank black tiles
//! if `n` is not a multiple of `cols`.
//!
//! The caller (Whitebeard agent) is responsible for ordering photos by
//! content_hash before passing them here, to ensure stable tile indices.

use std::io::{self, Read};
use std::os::raw::c_int;
use std::path::{Path, PathBuf};

use image::{DynamicImage, GenericImageView, RgbImage};
use rayon::prelude::*;
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
struct PhotoInput {
    path: PathBuf,
    ext: String,
    #[serde(default)]
    orientation: Option<u8>,
    content_hash: String,
}

#[derive(Deserialize)]
struct Input {
    photos: Vec<PhotoInput>,
    tile_size: u32,
    fit: String,
    quality: u8,
    output: PathBuf,
}

#[derive(Serialize)]
struct Output {
    cols: u32,
    rows: u32,
    #[serde(rename = "tileSize")]
    tile_size: u32,
    #[serde(rename = "photoOrder")]
    photo_order: Vec<String>,
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
    let n = input.photos.len();
    if n == 0 {
        return Err("no photos provided".into());
    }

    let tile_size = input.tile_size;
    let fit = input.fit.clone();

    // Phase 1: Decode + resize + fit in parallel (pure Rust, no libavif calls).
    let rgb_images: Vec<RgbImage> = input.photos
        .par_iter()
        .map(|photo| decode_and_prepare(&photo.path, &photo.ext, photo.orientation, tile_size, &fit))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e: String| -> Box<dyn std::error::Error> { e.into() })?;

    // Compute grid dimensions: square-ish, minimising wasted space.
    let (cols, rows) = grid_dims(n);
    let total_cells = (cols * rows) as usize;

    // Phase 2: Convert to YUV420 (sequentially — libavif is not thread-safe).
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
        let avif_img = if i < n {
            rgb_to_yuv420(&rgb_images[i])?
        } else {
            // Padding: black tile of the same size.
            let black = RgbImage::new(tile_size, tile_size);
            rgb_to_yuv420(&black)?
        };
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
        (*encoder).quality_alpha = 100; // no alpha channel
        (*encoder).speed = 6;           // balanced encode speed (0=slowest, 10=fastest)
        (*encoder).max_threads = std::thread::available_parallelism()
            .map(|n| n.get() as c_int)
            .unwrap_or(1);
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

    let photo_order: Vec<String> = input.photos.iter().map(|p| p.content_hash.clone()).collect();

    // guard and _enc_guard drop here, freeing all avifImages and the encoder.
    Ok(Output { cols, rows, tile_size, photo_order })
}

// ---------------------------------------------------------------------------
// Decode + prepare pipeline
// ---------------------------------------------------------------------------

/// Decode a photo file and produce a `tile_size × tile_size` RGB tile.
///
/// Errors are returned as `String` so that rayon's `par_iter` can propagate
/// them across threads.
fn decode_and_prepare(
    path: &Path,
    ext: &str,
    orientation: Option<u8>,
    tile_size: u32,
    fit: &str,
) -> Result<RgbImage, String> {
    let img = decode_photo(path, ext)?;
    let img = apply_orientation(img, orientation);
    Ok(if fit == "crop" {
        // Resize so short edge == tile_size, then center-crop to square.
        let img = resize_short_edge(img, tile_size);
        fit_crop(img, tile_size)
    } else {
        // "pad": resize so long edge == tile_size, then letterbox to square.
        let img = resize_long_edge(img, tile_size);
        fit_pad(img, tile_size)
    })
}

/// Decode a photo from disk, dispatching by file extension.
fn decode_photo(path: &Path, ext: &str) -> Result<DynamicImage, String> {
    match ext.to_lowercase().as_str() {
        ".cr2" | ".cr3" | ".nef" | ".arw" | ".dng" | ".raf" | ".orf" | ".rw2" | ".pef" => {
            decode_raw(path)
        }
        ".heic" | ".heif" => decode_heic(path),
        _ => image::open(path)
            .map_err(|e| format!("failed to open {}: {e}", path.display())),
    }
}

/// Apply a TIFF orientation value (1–8) to a decoded image.
/// `None` or `Some(1)` → no transform.
fn apply_orientation(img: DynamicImage, orientation: Option<u8>) -> DynamicImage {
    match orientation {
        None | Some(1) => img,
        Some(2) => img.fliph(),
        Some(3) => img.rotate180(),
        Some(4) => img.flipv(),
        Some(5) => img.rotate90().fliph(),  // transpose (flip over main diagonal)
        Some(6) => img.rotate90(),          // 90° CW
        Some(7) => img.rotate90().flipv(),  // transverse (flip over anti-diagonal)
        Some(8) => img.rotate270(),         // 90° CCW
        _ => img,
    }
}

/// Resize so the short edge equals `size` (preserves aspect ratio).
fn resize_short_edge(img: DynamicImage, size: u32) -> DynamicImage {
    let (w, h) = img.dimensions();
    let (new_w, new_h) = if w <= h {
        let new_h = (h as f64 * size as f64 / w as f64).round() as u32;
        (size, new_h.max(1))
    } else {
        let new_w = (w as f64 * size as f64 / h as f64).round() as u32;
        (new_w.max(1), size)
    };
    img.resize_exact(new_w, new_h, image::imageops::FilterType::Lanczos3)
}

/// Resize so the long edge equals `size` (preserves aspect ratio).
/// No-op if the image already fits within `size × size`.
fn resize_long_edge(img: DynamicImage, size: u32) -> DynamicImage {
    let (w, h) = img.dimensions();
    if w.max(h) <= size {
        return img;
    }
    let (new_w, new_h) = if w >= h {
        let new_h = (h as f64 * size as f64 / w as f64).round() as u32;
        (size, new_h.max(1))
    } else {
        let new_w = (w as f64 * size as f64 / h as f64).round() as u32;
        (new_w.max(1), size)
    };
    img.resize_exact(new_w, new_h, image::imageops::FilterType::Lanczos3)
}

/// Center-crop a (short_edge == size) image to `size × size`.
fn fit_crop(img: DynamicImage, size: u32) -> RgbImage {
    let (w, h) = img.dimensions();
    let left = w.saturating_sub(size) / 2;
    let top = h.saturating_sub(size) / 2;
    img.crop_imm(left, top, size, size).into_rgb8()
}

/// Paste a (long_edge == size) image centered on a black `size × size` canvas.
fn fit_pad(img: DynamicImage, size: u32) -> RgbImage {
    let rgb = img.into_rgb8();
    let (fw, fh) = rgb.dimensions();
    let mut canvas = RgbImage::new(size, size); // zero-initialised = black
    let paste_x = (size.saturating_sub(fw)) / 2;
    let paste_y = (size.saturating_sub(fh)) / 2;
    image::imageops::overlay(&mut canvas, &rgb, paste_x as i64, paste_y as i64);
    canvas
}

// ---------------------------------------------------------------------------
// Format-specific decoders
// ---------------------------------------------------------------------------

/// Decode a RAW photo (CR2, NEF, ARW, DNG, RAF, ORF, RW2, PEF).
///
/// Requires the `raw` Cargo feature (`--features raw`).
#[cfg(feature = "raw")]
fn decode_raw(_path: &Path) -> Result<DynamicImage, String> {
    // TODO: implement via the rawler crate once the API stabilises (pre-1.0).
    // Typical flow:
    //   let raw = rawler::decode_file(path)?;
    //   let rgb = raw.develop()?;
    //   Ok(DynamicImage::ImageRgb8(rgb))
    Err("RAW decode not yet implemented (--features raw stub)".into())
}

#[cfg(not(feature = "raw"))]
fn decode_raw(path: &Path) -> Result<DynamicImage, String> {
    Err(format!(
        "RAW format not supported; rebuild with --features raw: {}",
        path.display()
    ))
}

/// Decode a HEIC/HEIF photo.
///
/// Requires the `heic` Cargo feature (`--features heic`).
/// On Linux, install libheif: `apt install libheif-dev`.
/// On macOS, install libheif: `brew install libheif`.
#[cfg(feature = "heic")]
fn decode_heic(_path: &Path) -> Result<DynamicImage, String> {
    // TODO: implement via libheif-rs once tested.
    // use libheif_rs::{HeifContext, ColorSpace, RgbChroma};
    // let ctx = HeifContext::read_from_file(path)?;
    // let handle = ctx.primary_image_handle()?;
    // let image = handle.decode(ColorSpace::Rgb(RgbChroma::Rgb), false)?;
    // ...
    Err("HEIC decode not yet implemented (--features heic stub)".into())
}

#[cfg(not(feature = "heic"))]
fn decode_heic(path: &Path) -> Result<DynamicImage, String> {
    Err(format!(
        "HEIC/HEIF format not supported; rebuild with --features heic: {}",
        path.display()
    ))
}

// ---------------------------------------------------------------------------
// Grid geometry
// ---------------------------------------------------------------------------

/// Compute (cols, rows) for a square-ish grid holding `n` tiles.
fn grid_dims(n: usize) -> (u32, u32) {
    let cols = (n as f64).sqrt().ceil() as u32;
    let rows = n.div_ceil(cols as usize) as u32;
    (cols, rows)
}

// ---------------------------------------------------------------------------
// libavif helper
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Unit tests — pure Rust logic only (no libavif calls)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use image::{DynamicImage, RgbImage, Rgb};

    fn solid(w: u32, h: u32, pixel: Rgb<u8>) -> DynamicImage {
        let mut img = RgbImage::new(w, h);
        for p in img.pixels_mut() {
            *p = pixel;
        }
        DynamicImage::ImageRgb8(img)
    }

    fn landscape() -> DynamicImage { solid(400, 200, Rgb([200, 100, 50])) }
    fn portrait()  -> DynamicImage { solid(200, 400, Rgb([50, 100, 200])) }
    fn square()    -> DynamicImage { solid(300, 300, Rgb([128, 128, 128])) }

    // --- grid_dims -----------------------------------------------------------

    #[test]
    fn grid_dims_one() {
        assert_eq!(grid_dims(1), (1, 1));
    }

    #[test]
    fn grid_dims_two() {
        assert_eq!(grid_dims(2), (2, 1));
    }

    #[test]
    fn grid_dims_four() {
        assert_eq!(grid_dims(4), (2, 2));
    }

    #[test]
    fn grid_dims_five() {
        // ceil(sqrt(5)) = 3, ceil(5/3) = 2
        assert_eq!(grid_dims(5), (3, 2));
    }

    #[test]
    fn grid_dims_nine() {
        assert_eq!(grid_dims(9), (3, 3));
    }

    #[test]
    fn grid_dims_ten() {
        // ceil(sqrt(10)) = 4, ceil(10/4) = 3
        assert_eq!(grid_dims(10), (4, 3));
    }

    // --- apply_orientation ---------------------------------------------------

    #[test]
    fn orientation_none_is_noop() {
        let img = landscape();
        let out = apply_orientation(img, None);
        assert_eq!(out.dimensions(), (400, 200));
    }

    #[test]
    fn orientation_1_is_noop() {
        let img = landscape();
        let out = apply_orientation(img, Some(1));
        assert_eq!(out.dimensions(), (400, 200));
    }

    #[test]
    fn orientation_6_rotates_90_cw() {
        // 400×200 landscape rotated 90° CW → 200×400
        let img = landscape();
        let out = apply_orientation(img, Some(6));
        assert_eq!(out.dimensions(), (200, 400));
    }

    #[test]
    fn orientation_8_rotates_90_ccw() {
        let img = landscape();
        let out = apply_orientation(img, Some(8));
        assert_eq!(out.dimensions(), (200, 400));
    }

    #[test]
    fn orientation_3_rotates_180() {
        let img = landscape();
        let out = apply_orientation(img, Some(3));
        assert_eq!(out.dimensions(), (400, 200));
    }

    #[test]
    fn orientation_2_flips_h() {
        let img = landscape();
        let out = apply_orientation(img, Some(2));
        assert_eq!(out.dimensions(), (400, 200));
    }

    #[test]
    fn orientation_4_flips_v() {
        let img = landscape();
        let out = apply_orientation(img, Some(4));
        assert_eq!(out.dimensions(), (400, 200));
    }

    #[test]
    fn orientation_5_transposes() {
        // rotate90 then fliph → 200×400
        let img = landscape();
        let out = apply_orientation(img, Some(5));
        assert_eq!(out.dimensions(), (200, 400));
    }

    #[test]
    fn orientation_7_transverses() {
        let img = landscape();
        let out = apply_orientation(img, Some(7));
        assert_eq!(out.dimensions(), (200, 400));
    }

    #[test]
    fn orientation_unknown_is_noop() {
        let img = landscape();
        let out = apply_orientation(img, Some(9));
        assert_eq!(out.dimensions(), (400, 200));
    }

    // --- resize_short_edge ---------------------------------------------------

    #[test]
    fn resize_short_edge_landscape_short_is_height() {
        // 400×200, short edge = 200 → target 128 → scale 128/200
        // new_w = round(400 * 128/200) = 256, new_h = 128
        let out = resize_short_edge(landscape(), 128);
        assert_eq!(out.height(), 128);
        assert!(out.width() > out.height());
    }

    #[test]
    fn resize_short_edge_portrait_short_is_width() {
        // 200×400, short edge = 200 → target 128
        // new_w = 128, new_h = round(400 * 128/200) = 256
        let out = resize_short_edge(portrait(), 128);
        assert_eq!(out.width(), 128);
        assert!(out.height() > out.width());
    }

    #[test]
    fn resize_short_edge_square() {
        let out = resize_short_edge(square(), 64);
        assert_eq!(out.dimensions(), (64, 64));
    }

    // --- resize_long_edge ----------------------------------------------------

    #[test]
    fn resize_long_edge_landscape_long_is_width() {
        // 400×200, long edge = 400 → target 256
        // new_w = 256, new_h = round(200 * 256/400) = 128
        let out = resize_long_edge(landscape(), 256);
        assert_eq!(out.width(), 256);
        assert!(out.height() < out.width());
    }

    #[test]
    fn resize_long_edge_portrait_long_is_height() {
        let out = resize_long_edge(portrait(), 256);
        assert_eq!(out.height(), 256);
        assert!(out.width() < out.height());
    }

    #[test]
    fn resize_long_edge_already_fits_is_noop() {
        // 400×200 already fits within 512 → no resize
        let out = resize_long_edge(landscape(), 512);
        assert_eq!(out.dimensions(), (400, 200));
    }

    #[test]
    fn resize_long_edge_square_equal_size_is_noop() {
        let out = resize_long_edge(square(), 300);
        assert_eq!(out.dimensions(), (300, 300));
    }

    // --- fit_crop ------------------------------------------------------------

    #[test]
    fn fit_crop_output_is_square_tile() {
        let img = resize_short_edge(landscape(), 128);
        let out = fit_crop(img, 128);
        assert_eq!(out.dimensions(), (128, 128));
    }

    #[test]
    fn fit_crop_portrait_output_is_square_tile() {
        let img = resize_short_edge(portrait(), 64);
        let out = fit_crop(img, 64);
        assert_eq!(out.dimensions(), (64, 64));
    }

    #[test]
    fn fit_crop_already_square() {
        let img = resize_short_edge(square(), 100);
        let out = fit_crop(img, 100);
        assert_eq!(out.dimensions(), (100, 100));
    }

    // --- fit_pad -------------------------------------------------------------

    #[test]
    fn fit_pad_output_is_square_tile() {
        let img = resize_long_edge(landscape(), 128);
        let out = fit_pad(img, 128);
        assert_eq!(out.dimensions(), (128, 128));
    }

    #[test]
    fn fit_pad_portrait_output_is_square_tile() {
        let img = resize_long_edge(portrait(), 64);
        let out = fit_pad(img, 64);
        assert_eq!(out.dimensions(), (64, 64));
    }

    #[test]
    fn fit_pad_corners_are_black() {
        // After padding a landscape, the top-left corner should be black.
        let img = resize_long_edge(landscape(), 128);
        let out = fit_pad(img, 128);
        // Landscape 400×200 resized to 128×64; paste_y = (128-64)/2 = 32.
        // Pixel at (0, 0) is outside the pasted region → black.
        assert_eq!(out.get_pixel(0, 0), &Rgb([0, 0, 0]));
    }

    #[test]
    fn fit_pad_center_is_not_black() {
        // Centre of a padded non-black image should carry original colour.
        let img = resize_long_edge(solid(400, 200, Rgb([255, 0, 0])), 128);
        let out = fit_pad(img, 128);
        // The red image occupies rows 32..96; centre pixel (64, 64) is red.
        let p = out.get_pixel(64, 64);
        assert_ne!(p, &Rgb([0, 0, 0]), "centre should be non-black");
    }
}
