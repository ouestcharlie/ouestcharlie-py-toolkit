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

use std::io::{self};
use std::path::{Path, PathBuf};

use image::{DynamicImage, GenericImageView, RgbImage};
use rayon::prelude::*;
use rgb::FromSlice;
use serde::{Deserialize, Serialize};

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

/// Response envelope: either a successful result or an in-band error.
#[derive(Serialize)]
#[serde(untagged)]
enum Response {
    AvifGrid(AvifGridOutput),
    JpegPreview(JpegPreviewOutput),
    Error { error: String },
}

fn main() {
    use std::io::BufRead;

    // Support `--version` for version negotiation with the Python toolkit.
    if std::env::args().nth(1).as_deref() == Some("--version") {
        println!("image-proc {}", env!("CARGO_PKG_VERSION"));
        return;
    }

    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = io::BufWriter::new(stdout.lock());

    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(e) => {
                eprintln!("image-proc: failed to read line: {e}");
                break;
            }
        };
        if line.trim().is_empty() {
            continue;
        }

        let response = match serde_json::from_str::<Request>(&line) {
            Err(e) => Response::Error { error: format!("invalid JSON input: {e}") },
            Ok(Request::AvifGrid(input)) => match run_avif_grid(input) {
                Ok(out) => Response::AvifGrid(out),
                Err(e) => Response::Error { error: e.to_string() },
            },
            Ok(Request::JpegPreview(input)) => match run_jpeg_preview(input) {
                Ok(out) => Response::JpegPreview(out),
                Err(e) => Response::Error { error: e.to_string() },
            },
        };

        let json = serde_json::to_string(&response).unwrap();
        use std::io::Write;
        if let Err(e) = writeln!(out, "{json}") {
            eprintln!("image-proc: failed to write response: {e}");
            break;
        }
        if let Err(e) = out.flush() {
            eprintln!("image-proc: failed to flush: {e}");
            break;
        }
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
    let (cols, rows) = grid_dims(n);
    let total_cells = (cols * rows) as usize;

    // Phase 1: Decode + resize + fit in parallel.
    let mut tiles: Vec<RgbImage> = input.photos
        .par_iter()
        .map(|photo| decode_and_prepare(&photo.path, &photo.ext, photo.orientation, tile_size, &fit))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e: String| -> Box<dyn std::error::Error> { e.into() })?;

    // Pad last row with blank black tiles.
    while tiles.len() < total_cells {
        tiles.push(RgbImage::new(tile_size, tile_size));
    }

    // Phase 2: Composite all tiles into a single canvas.
    let total_w = cols * tile_size;
    let total_h = rows * tile_size;
    let mut canvas = RgbImage::new(total_w, total_h);
    for (i, tile) in tiles.iter().enumerate() {
        let col = (i as u32) % cols;
        let row = (i as u32) / cols;
        image::imageops::overlay(&mut canvas, tile, (col * tile_size) as i64, (row * tile_size) as i64);
    }

    // Phase 3: Encode as AVIF using ravif (pure Rust, no system deps).
    let pixels: &[rgb::RGB8] = canvas.as_raw().as_rgb();
    let img = ravif::Img::new(pixels, total_w as usize, total_h as usize);
    let encoded = ravif::Encoder::new()
        .with_quality(input.quality as f32)
        .with_speed(6)
        .encode_rgb(img)
        .map_err(|e| format!("AVIF encoding failed: {e}"))?;

    std::fs::write(&input.output, &encoded.avif_file)?;

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

    // Helper: write a solid-color JPEG to a temp path and return a PhotoInput.
    fn make_jpeg(name: &str, w: u32, h: u32, px: Rgb<u8>, hash: &str) -> PhotoInput {
        let path = std::env::temp_dir().join(name);
        solid(w, h, px).save(&path).unwrap();
        PhotoInput { path, ext: ".jpg".into(), orientation: None, content_hash: hash.into() }
    }

    #[test]
    fn avif_grid_single_photo_produces_valid_file() {
        let output = std::env::temp_dir().join("avif_grid_1.avif");
        let result = run_avif_grid(AvifGridInput {
            photos: vec![make_jpeg("ag1_a.jpg", 300, 300, Rgb([200, 100, 50]), "sha256:aaaa")],
            tile_size: 64,
            fit: "crop".into(),
            quality: 55,
            output: output.clone(),
        }).unwrap();
        assert_eq!(result.cols, 1);
        assert_eq!(result.rows, 1);
        assert_eq!(result.tile_size, 64);
        assert_eq!(result.photo_order, vec!["sha256:aaaa"]);
        assert!(output.exists());
        assert!(output.metadata().unwrap().len() > 0, "output file should be non-empty");
    }

    #[test]
    fn avif_grid_four_photos_two_by_two() {
        let output = std::env::temp_dir().join("avif_grid_4.avif");
        let photos = vec![
            make_jpeg("ag4_a.jpg", 200, 200, Rgb([255,   0,   0]), "sha256:a1"),
            make_jpeg("ag4_b.jpg", 200, 200, Rgb([  0, 255,   0]), "sha256:a2"),
            make_jpeg("ag4_c.jpg", 200, 200, Rgb([  0,   0, 255]), "sha256:a3"),
            make_jpeg("ag4_d.jpg", 200, 200, Rgb([255, 255,   0]), "sha256:a4"),
        ];
        let hashes: Vec<String> = photos.iter().map(|p| p.content_hash.clone()).collect();
        let result = run_avif_grid(AvifGridInput {
            photos,
            tile_size: 32,
            fit: "crop".into(),
            quality: 55,
            output: output.clone(),
        }).unwrap();
        assert_eq!(result.cols, 2);
        assert_eq!(result.rows, 2);
        assert_eq!(result.photo_order, hashes);
        assert!(output.exists());
    }

    #[test]
    fn avif_grid_five_photos_pads_last_row() {
        // 5 photos → cols=3, rows=2 → 6 cells (1 padding tile)
        let output = std::env::temp_dir().join("avif_grid_5.avif");
        let photos: Vec<PhotoInput> = (0..5).map(|i| {
            make_jpeg(&format!("ag5_{i}.jpg"), 100, 100, Rgb([i * 50, 100, 200]), &format!("sha256:b{i}"))
        }).collect();
        let result = run_avif_grid(AvifGridInput {
            photos,
            tile_size: 16,
            fit: "pad".into(),
            quality: 55,
            output: output.clone(),
        }).unwrap();
        assert_eq!(result.cols, 3);
        assert_eq!(result.rows, 2);
        assert_eq!(result.photo_order.len(), 5);
        assert!(output.exists());
    }

    #[test]
    fn avif_grid_photo_order_matches_input_hashes() {
        let output = std::env::temp_dir().join("avif_grid_order.avif");
        let hashes = vec!["sha256:z3", "sha256:a1", "sha256:m2"];
        let photos: Vec<PhotoInput> = hashes.iter().enumerate().map(|(i, h)| {
            make_jpeg(&format!("ago_{i}.jpg"), 80, 80, Rgb([100, 100, 100]), h)
        }).collect();
        let result = run_avif_grid(AvifGridInput {
            photos,
            tile_size: 16,
            fit: "crop".into(),
            quality: 55,
            output,
        }).unwrap();
        // photo_order must echo input hashes in input order (caller controls ordering)
        assert_eq!(result.photo_order, hashes);
    }

    #[test]
    fn avif_grid_empty_photos_returns_error() {
        let output = std::env::temp_dir().join("avif_grid_empty.avif");
        let err = run_avif_grid(AvifGridInput {
            photos: vec![],
            tile_size: 64,
            fit: "crop".into(),
            quality: 55,
            output,
        });
        assert!(err.is_err());
    }
}
