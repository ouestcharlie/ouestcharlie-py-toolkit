//! image-proc — image processing coprocessor for OuEstCharlie.
//!
//! Reads a JSON object from stdin and dispatches to one of two commands
//! based on the shape of the input:
//!
//! # Command: avif_grid (when "photos" plural field is present)
//!
//! Decodes photos and assembles them into an AVIF grid container.
//!
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
//! `fit` is `"crop"` (center-crop to square) or `"pad"` (letterbox with black).
//!
//! Output:
//! ```json
//! {"cols": 32, "rows": 4, "tileSize": 256, "photoOrder": ["sha256:aaa...", ...]}
//! ```
//!
//! # Command: jpeg_preview (when "photo" singular field is present)
//!
//! Decodes a single photo, applies EXIF orientation, resizes to max_long_edge,
//! and saves as JPEG.
//!
//! ```json
//! {
//!   "photo": { "path": "/tmp/staged.cr2", "ext": ".cr2", "orientation": 6, "content_hash": "sha256:..." },
//!   "max_long_edge": 1440,
//!   "quality": 85,
//!   "output": "/tmp/preview.jpg"
//! }
//! ```
//!
//! Output:
//! ```json
//! {"width": 1440, "height": 960}
//! ```
//!
//! # Grid layout (avif_grid)
//!
//! Photos are arranged in a square-ish grid: `cols = ceil(sqrt(n))`,
//! `rows = ceil(n / cols)`.  The last row is padded with blank black tiles.
//!
//! The caller is responsible for ordering photos by content_hash before
//! passing them here, to ensure stable tile indices.

use std::io::{self, Read};
use std::os::raw::c_int;
use std::path::{Path, PathBuf};

use image::{DynamicImage, GenericImageView, RgbImage};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Minimal libavif FFI — mirrors avif.h from libavif ≥ 1.0.0
// ---------------------------------------------------------------------------

mod ffi {
    use std::os::raw::c_int;

    #[repr(C)]
    pub struct AvifImage { _opaque: [u8; 0] }

    #[repr(C)]
    pub struct AvifEncoder {
        pub codec_choice: c_int,
        pub max_threads: c_int,
        pub speed: c_int,
        _keyframe_interval: c_int,
        _timescale: u64,
        _repetition_count: c_int,
        _extra_layer_count: u32,
        pub quality: c_int,
        pub quality_alpha: c_int,
    }

    #[repr(C)]
    pub struct AvifRgbImage {
        pub width: u32,
        pub height: u32,
        pub depth: u32,
        pub format: u32,
        pub chroma_upsampling: u32,
        pub chroma_downsampling: u32,
        pub avoid_libyuv: c_int,
        pub ignore_alpha: c_int,
        pub alpha_premultiplied: c_int,
        pub is_float: c_int,
        pub max_threads: c_int,
        pub pixels: *mut u8,
        pub row_bytes: u32,
    }

    #[repr(C)]
    pub struct AvifRwData { pub data: *mut u8, pub size: usize }

    impl Default for AvifRwData {
        fn default() -> Self { AvifRwData { data: std::ptr::null_mut(), size: 0 } }
    }

    pub const AVIF_RESULT_OK: c_int = 0;
    pub const AVIF_PIXEL_FORMAT_YUV420: u32 = 3;
    pub const AVIF_RGB_FORMAT_RGB: u32 = 0;
    pub const AVIF_CHROMA_UPSAMPLING_AUTOMATIC: u32 = 0;
    pub const AVIF_CHROMA_DOWNSAMPLING_AUTOMATIC: u32 = 0;
    pub const AVIF_ADD_IMAGE_FLAG_SINGLE: u32 = 2;

    extern "C" {
        pub fn avifImageCreate(width: u32, height: u32, depth: u32, yuv_format: u32) -> *mut AvifImage;
        pub fn avifImageDestroy(image: *mut AvifImage);
        pub fn avifImageRGBToYUV(image: *mut AvifImage, rgb: *const AvifRgbImage) -> c_int;
        pub fn avifEncoderCreate() -> *mut AvifEncoder;
        pub fn avifEncoderDestroy(encoder: *mut AvifEncoder);
        pub fn avifEncoderAddImageGrid(
            encoder: *mut AvifEncoder,
            grid_cols: u32,
            grid_rows: u32,
            cell_images: *const *const AvifImage,
            add_image_flags: u32,
        ) -> c_int;
        pub fn avifEncoderFinish(encoder: *mut AvifEncoder, output: *mut AvifRwData) -> c_int;
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

/// Dispatch by shape: presence of "photos" (plural) → AvifGrid;
/// presence of "photo" (singular) → JpegPreview.
#[derive(Deserialize)]
#[serde(untagged)]
enum Request {
    AvifGrid(AvifGridInput),
    JpegPreview(JpegPreviewInput),
}

#[derive(Deserialize)]
struct AvifGridInput {
    photos: Vec<PhotoInput>,
    tile_size: u32,
    fit: String,
    quality: u8,
    output: PathBuf,
}

#[derive(Deserialize)]
struct JpegPreviewInput {
    photo: PhotoInput,
    max_long_edge: u32,
    quality: u8,
    output: PathBuf,
}

#[derive(Serialize)]
struct AvifGridOutput {
    cols: u32,
    rows: u32,
    #[serde(rename = "tileSize")]
    tile_size: u32,
    #[serde(rename = "photoOrder")]
    photo_order: Vec<String>,
}

#[derive(Serialize)]
struct JpegPreviewOutput {
    width: u32,
    height: u32,
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

fn main() {
    let mut stdin = String::new();
    io::stdin().read_to_string(&mut stdin).expect("failed to read stdin");

    let request: Request = match serde_json::from_str(&stdin) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("image-proc error: invalid JSON input: {e}");
            std::process::exit(1);
        }
    };

    match request {
        Request::AvifGrid(input) => match run_avif_grid(input) {
            Ok(out) => println!("{}", serde_json::to_string(&out).unwrap()),
            Err(e) => { eprintln!("image-proc error: {e}"); std::process::exit(1); }
        },
        Request::JpegPreview(input) => match run_jpeg_preview(input) {
            Ok(out) => println!("{}", serde_json::to_string(&out).unwrap()),
            Err(e) => { eprintln!("image-proc error: {e}"); std::process::exit(1); }
        },
    }
}

// ---------------------------------------------------------------------------
// Command: avif_grid
// ---------------------------------------------------------------------------

fn run_avif_grid(input: AvifGridInput) -> Result<AvifGridOutput, Box<dyn std::error::Error>> {
    let n = input.photos.len();
    if n == 0 {
        return Err("no photos provided".into());
    }

    let tile_size = input.tile_size;
    let fit = input.fit.clone();

    // Phase 1: Decode + resize + fit in parallel.
    let rgb_images: Vec<RgbImage> = input.photos
        .par_iter()
        .map(|photo| decode_and_prepare(&photo.path, &photo.ext, photo.orientation, tile_size, &fit))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e: String| -> Box<dyn std::error::Error> { e.into() })?;

    let (cols, rows) = grid_dims(n);
    let total_cells = (cols * rows) as usize;

    // Phase 2: Convert to YUV420 (libavif is not thread-safe).
    struct CellGuard(Vec<*mut AvifImage>);
    impl Drop for CellGuard {
        fn drop(&mut self) { for p in &self.0 { unsafe { avifImageDestroy(*p) }; } }
    }
    let mut guard = CellGuard(Vec::with_capacity(total_cells));

    for i in 0..total_cells {
        let avif_img = if i < n {
            rgb_to_yuv420(&rgb_images[i])?
        } else {
            rgb_to_yuv420(&RgbImage::new(tile_size, tile_size))?
        };
        guard.0.push(avif_img);
    }

    let const_ptrs: Vec<*const AvifImage> = guard.0.iter().map(|p| *p as *const _).collect();

    let encoder = unsafe { avifEncoderCreate() };
    if encoder.is_null() { return Err("avifEncoderCreate returned null".into()); }
    struct EncoderGuard(*mut AvifEncoder);
    impl Drop for EncoderGuard { fn drop(&mut self) { unsafe { avifEncoderDestroy(self.0) }; } }
    let _enc_guard = EncoderGuard(encoder);

    unsafe {
        (*encoder).quality = input.quality as c_int;
        (*encoder).quality_alpha = 100;
        (*encoder).speed = 6;
        (*encoder).max_threads = std::thread::available_parallelism()
            .map(|n| n.get() as c_int).unwrap_or(1);
    }

    let rc = unsafe {
        avifEncoderAddImageGrid(encoder, cols, rows, const_ptrs.as_ptr(), AVIF_ADD_IMAGE_FLAG_SINGLE)
    };
    if rc != AVIF_RESULT_OK { return Err(format!("avifEncoderAddImageGrid failed (code {rc})").into()); }

    let mut output_data = AvifRwData::default();
    let rc = unsafe { avifEncoderFinish(encoder, &mut output_data) };
    if rc != AVIF_RESULT_OK { return Err(format!("avifEncoderFinish failed (code {rc})").into()); }

    let avif_bytes = unsafe { std::slice::from_raw_parts(output_data.data, output_data.size) }.to_vec();
    unsafe { avifRWDataFree(&mut output_data) };

    std::fs::write(&input.output, &avif_bytes)?;

    let photo_order: Vec<String> = input.photos.iter().map(|p| p.content_hash.clone()).collect();
    Ok(AvifGridOutput { cols, rows, tile_size, photo_order })
}

// ---------------------------------------------------------------------------
// Command: jpeg_preview
// ---------------------------------------------------------------------------

fn run_jpeg_preview(input: JpegPreviewInput) -> Result<JpegPreviewOutput, Box<dyn std::error::Error>> {
    let photo = &input.photo;
    let img = decode_photo(&photo.path, &photo.ext)
        .map_err(|e| -> Box<dyn std::error::Error> { e.into() })?;
    let img = apply_orientation(img, photo.orientation);
    let img = resize_long_edge(img, input.max_long_edge);
    let (width, height) = img.dimensions();

    let file = std::fs::File::create(&input.output)?;
    let mut writer = std::io::BufWriter::new(file);
    let mut encoder = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut writer, input.quality);
    encoder.encode_image(&img.into_rgb8())?;

    Ok(JpegPreviewOutput { width, height })
}

// ---------------------------------------------------------------------------
// Decode + prepare pipeline
// ---------------------------------------------------------------------------

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
        fit_crop(resize_short_edge(img, tile_size), tile_size)
    } else {
        fit_pad(resize_long_edge(img, tile_size), tile_size)
    })
}

fn decode_photo(path: &Path, ext: &str) -> Result<DynamicImage, String> {
    match ext.to_lowercase().as_str() {
        ".cr2" | ".cr3" | ".nef" | ".arw" | ".dng" | ".raf" | ".orf" | ".rw2" | ".pef" => {
            decode_raw(path)
        }
        ".heic" | ".heif" => decode_heic(path),
        _ => image::open(path).map_err(|e| format!("failed to open {}: {e}", path.display())),
    }
}

fn apply_orientation(img: DynamicImage, orientation: Option<u8>) -> DynamicImage {
    match orientation {
        None | Some(1) => img,
        Some(2) => img.fliph(),
        Some(3) => img.rotate180(),
        Some(4) => img.flipv(),
        Some(5) => img.rotate90().fliph(),
        Some(6) => img.rotate90(),
        Some(7) => img.rotate90().flipv(),
        Some(8) => img.rotate270(),
        _ => img,
    }
}

fn resize_short_edge(img: DynamicImage, size: u32) -> DynamicImage {
    let (w, h) = img.dimensions();
    let (new_w, new_h) = if w <= h {
        (size, (h as f64 * size as f64 / w as f64).round() as u32)
    } else {
        ((w as f64 * size as f64 / h as f64).round() as u32, size)
    };
    img.resize_exact(new_w.max(1), new_h.max(1), image::imageops::FilterType::Lanczos3)
}

fn resize_long_edge(img: DynamicImage, size: u32) -> DynamicImage {
    let (w, h) = img.dimensions();
    if w.max(h) <= size { return img; }
    let (new_w, new_h) = if w >= h {
        (size, (h as f64 * size as f64 / w as f64).round() as u32)
    } else {
        ((w as f64 * size as f64 / h as f64).round() as u32, size)
    };
    img.resize_exact(new_w.max(1), new_h.max(1), image::imageops::FilterType::Lanczos3)
}

fn fit_crop(img: DynamicImage, size: u32) -> RgbImage {
    let (w, h) = img.dimensions();
    img.crop_imm(w.saturating_sub(size) / 2, h.saturating_sub(size) / 2, size, size).into_rgb8()
}

fn fit_pad(img: DynamicImage, size: u32) -> RgbImage {
    let rgb = img.into_rgb8();
    let (fw, fh) = rgb.dimensions();
    let mut canvas = RgbImage::new(size, size);
    image::imageops::overlay(&mut canvas, &rgb, (size.saturating_sub(fw) / 2) as i64, (size.saturating_sub(fh) / 2) as i64);
    canvas
}

// ---------------------------------------------------------------------------
// Format-specific decoders
// ---------------------------------------------------------------------------

#[cfg(feature = "raw")]
fn decode_raw(_path: &Path) -> Result<DynamicImage, String> {
    Err("RAW decode not yet implemented (--features raw stub)".into())
}

#[cfg(not(feature = "raw"))]
fn decode_raw(path: &Path) -> Result<DynamicImage, String> {
    Err(format!("RAW format not supported; rebuild with --features raw: {}", path.display()))
}

#[cfg(feature = "heic")]
fn decode_heic(_path: &Path) -> Result<DynamicImage, String> {
    Err("HEIC decode not yet implemented (--features heic stub)".into())
}

#[cfg(not(feature = "heic"))]
fn decode_heic(path: &Path) -> Result<DynamicImage, String> {
    Err(format!("HEIC/HEIF format not supported; rebuild with --features heic: {}", path.display()))
}

// ---------------------------------------------------------------------------
// Grid geometry
// ---------------------------------------------------------------------------

fn grid_dims(n: usize) -> (u32, u32) {
    let cols = (n as f64).sqrt().ceil() as u32;
    let rows = n.div_ceil(cols as usize) as u32;
    (cols, rows)
}

// ---------------------------------------------------------------------------
// libavif helper
// ---------------------------------------------------------------------------

fn rgb_to_yuv420(rgb: &RgbImage) -> Result<*mut AvifImage, Box<dyn std::error::Error>> {
    let w = rgb.width();
    let h = rgb.height();
    let avif_img = unsafe { avifImageCreate(w, h, 8, AVIF_PIXEL_FORMAT_YUV420) };
    if avif_img.is_null() { return Err("avifImageCreate returned null".into()); }

    let rgb_desc = AvifRgbImage {
        width: w, height: h, depth: 8,
        format: AVIF_RGB_FORMAT_RGB,
        chroma_upsampling: AVIF_CHROMA_UPSAMPLING_AUTOMATIC,
        chroma_downsampling: AVIF_CHROMA_DOWNSAMPLING_AUTOMATIC,
        avoid_libyuv: 0, ignore_alpha: 1, alpha_premultiplied: 0, is_float: 0,
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
// Unit tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use image::{DynamicImage, Rgb, RgbImage};

    fn solid(w: u32, h: u32, pixel: Rgb<u8>) -> DynamicImage {
        let mut img = RgbImage::new(w, h);
        for p in img.pixels_mut() { *p = pixel; }
        DynamicImage::ImageRgb8(img)
    }
    fn landscape() -> DynamicImage { solid(400, 200, Rgb([200, 100, 50])) }
    fn portrait()  -> DynamicImage { solid(200, 400, Rgb([50, 100, 200])) }
    fn square()    -> DynamicImage { solid(300, 300, Rgb([128, 128, 128])) }

    #[test] fn grid_dims_one()  { assert_eq!(grid_dims(1),  (1, 1)); }
    #[test] fn grid_dims_two()  { assert_eq!(grid_dims(2),  (2, 1)); }
    #[test] fn grid_dims_four() { assert_eq!(grid_dims(4),  (2, 2)); }
    #[test] fn grid_dims_five() { assert_eq!(grid_dims(5),  (3, 2)); }
    #[test] fn grid_dims_nine() { assert_eq!(grid_dims(9),  (3, 3)); }
    #[test] fn grid_dims_ten()  { assert_eq!(grid_dims(10), (4, 3)); }

    #[test] fn orientation_none_is_noop()   { assert_eq!(apply_orientation(landscape(), None).dimensions(),    (400, 200)); }
    #[test] fn orientation_1_is_noop()      { assert_eq!(apply_orientation(landscape(), Some(1)).dimensions(), (400, 200)); }
    #[test] fn orientation_6_rotates_90_cw(){ assert_eq!(apply_orientation(landscape(), Some(6)).dimensions(), (200, 400)); }
    #[test] fn orientation_8_rotates_90_ccw(){ assert_eq!(apply_orientation(landscape(), Some(8)).dimensions(),(200, 400)); }
    #[test] fn orientation_3_rotates_180() { assert_eq!(apply_orientation(landscape(), Some(3)).dimensions(), (400, 200)); }
    #[test] fn orientation_2_flips_h()     { assert_eq!(apply_orientation(landscape(), Some(2)).dimensions(), (400, 200)); }
    #[test] fn orientation_4_flips_v()     { assert_eq!(apply_orientation(landscape(), Some(4)).dimensions(), (400, 200)); }
    #[test] fn orientation_5_transposes()  { assert_eq!(apply_orientation(landscape(), Some(5)).dimensions(), (200, 400)); }
    #[test] fn orientation_7_transverses() { assert_eq!(apply_orientation(landscape(), Some(7)).dimensions(), (200, 400)); }
    #[test] fn orientation_unknown_is_noop(){ assert_eq!(apply_orientation(landscape(), Some(9)).dimensions(),(400, 200)); }

    #[test] fn resize_short_edge_landscape() { let out = resize_short_edge(landscape(), 128); assert_eq!(out.height(), 128); assert!(out.width() > out.height()); }
    #[test] fn resize_short_edge_portrait()  { let out = resize_short_edge(portrait(),  128); assert_eq!(out.width(), 128);  assert!(out.height() > out.width()); }
    #[test] fn resize_short_edge_square()    { assert_eq!(resize_short_edge(square(), 64).dimensions(), (64, 64)); }

    #[test] fn resize_long_edge_landscape()         { let out = resize_long_edge(landscape(), 256); assert_eq!(out.width(), 256); assert!(out.height() < out.width()); }
    #[test] fn resize_long_edge_portrait()          { let out = resize_long_edge(portrait(),  256); assert_eq!(out.height(), 256); assert!(out.width() < out.height()); }
    #[test] fn resize_long_edge_already_fits_is_noop() { assert_eq!(resize_long_edge(landscape(), 512).dimensions(), (400, 200)); }
    #[test] fn resize_long_edge_square_equal_size_is_noop() { assert_eq!(resize_long_edge(square(), 300).dimensions(), (300, 300)); }

    #[test] fn fit_crop_output_is_square_tile()     { assert_eq!(fit_crop(resize_short_edge(landscape(), 128), 128).dimensions(), (128, 128)); }
    #[test] fn fit_crop_portrait_output_is_square_tile() { assert_eq!(fit_crop(resize_short_edge(portrait(), 64), 64).dimensions(), (64, 64)); }

    #[test] fn fit_pad_output_is_square_tile() { assert_eq!(fit_pad(resize_long_edge(landscape(), 128), 128).dimensions(), (128, 128)); }
    #[test] fn fit_pad_corners_are_black() {
        let out = fit_pad(resize_long_edge(landscape(), 128), 128);
        assert_eq!(out.get_pixel(0, 0), &Rgb([0, 0, 0]));
    }
    #[test] fn fit_pad_center_is_not_black() {
        let out = fit_pad(resize_long_edge(solid(400, 200, Rgb([255, 0, 0])), 128), 128);
        assert_ne!(out.get_pixel(64, 64), &Rgb([0, 0, 0]), "centre should be non-black");
    }

    #[test]
    fn jpeg_preview_landscape_resizes_correctly() {
        let tmpdir = std::env::temp_dir();
        let input_path  = tmpdir.join("imgproc_test_in.jpg");
        let output_path = tmpdir.join("imgproc_test_out.jpg");
        // 2000×1000 landscape; after resize_long_edge(1440) → 1440×720
        solid(2000, 1000, Rgb([200, 100, 50])).save(&input_path).unwrap();
        let result = run_jpeg_preview(JpegPreviewInput {
            photo: PhotoInput { path: input_path, ext: ".jpg".into(), orientation: None, content_hash: "sha256:t".into() },
            max_long_edge: 1440, quality: 85, output: output_path.clone(),
        }).unwrap();
        assert_eq!(result.width, 1440);
        assert_eq!(result.height, 720);
        assert!(output_path.exists());
    }

    #[test]
    fn jpeg_preview_small_image_unchanged() {
        let tmpdir = std::env::temp_dir();
        let input_path  = tmpdir.join("imgproc_small_in.jpg");
        let output_path = tmpdir.join("imgproc_small_out.jpg");
        // 800×600 — fits within 1440 → unchanged
        solid(800, 600, Rgb([100, 150, 200])).save(&input_path).unwrap();
        let result = run_jpeg_preview(JpegPreviewInput {
            photo: PhotoInput { path: input_path, ext: ".jpg".into(), orientation: None, content_hash: "sha256:t2".into() },
            max_long_edge: 1440, quality: 85, output: output_path,
        }).unwrap();
        assert_eq!(result.width, 800);
        assert_eq!(result.height, 600);
    }
}
