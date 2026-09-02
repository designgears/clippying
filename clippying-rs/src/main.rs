#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use slint::{ModelRc, SharedString, VecModel};
use serde_json::json;
use log::{error, info, warn};
use std::cell::RefCell;
use std::io::{Read, Write};
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use pulse::sample::{Format, Spec};
use pulse::stream::Direction;
use psimple::Simple;

use clippying::daemon;
use clippying::gain::{self, GainCell};

/// Number of buckets in the precomputed peak envelope. Small enough that a
/// selection's peak level can be recomputed on every drag event.
const ENVELOPE_BUCKETS: usize = 4096;

struct AudioClip {
    samples: Vec<i16>,
    sample_rate: u32,
}

#[derive(Clone)]
struct SaveTarget {
    clips_dir: Option<PathBuf>,
    source: Option<String>,
    mode: SaveMode,
}

#[derive(Clone, Copy)]
enum SaveMode {
    TrimToFile,
    EmitSelection,
}

/// How the trimmer window should open.
struct TrimmerOptions {
    preview_sink: Option<String>,
    save_target: SaveTarget,
    initial_selection_secs: Option<(f32, f32)>,
    gain_db: f32,
}

impl AudioClip {
    fn duration_secs(&self) -> f32 {
        self.samples.len() as f32 / self.sample_rate as f32
    }
}

slint::include_modules!();

fn main() {
    init_logging();

    let exe = std::env::current_exe()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| "clippying".to_string());

    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("start") => {
            info!("starting daemon");
            daemon::daemonize_manager(&exe);
        }
        Some("stop") => {
            info!("stopping daemon");
            daemon::stop_manager();
        }
        Some("restart") => {
            info!("restarting daemon");
            daemon::stop_manager();
            std::thread::sleep(std::time::Duration::from_millis(200));
            daemon::daemonize_manager(&exe);
        }
        Some("--stdin-pcm") => {
            let sample_rate = args
                .next()
                .and_then(|s| s.parse::<u32>().ok())
                .unwrap_or(48_000);
            let channels = args
                .next()
                .and_then(|s| s.parse::<u8>().ok())
                .unwrap_or(1);

            let mut preview_sink: Option<String> = None;
            let mut clips_dir: Option<PathBuf> = None;
            let mut source: Option<String> = None;
            let mut save_mode = SaveMode::TrimToFile;
            let mut selection_start_secs: Option<f32> = None;
            let mut selection_end_secs: Option<f32> = None;
            let mut gain_db = 0.0f32;
            while let Some(a) = args.next() {
                if a == "--preview-sink" {
                    preview_sink = args.next();
                } else if a == "--clips-dir" {
                    clips_dir = args.next().map(PathBuf::from);
                } else if a == "--source" {
                    source = args.next();
                } else if a == "--emit-selection" {
                    save_mode = SaveMode::EmitSelection;
                } else if a == "--selection-start" {
                    selection_start_secs = args.next().and_then(|s| s.parse::<f32>().ok());
                } else if a == "--selection-end" {
                    selection_end_secs = args.next().and_then(|s| s.parse::<f32>().ok());
                } else if a == "--gain" {
                    gain_db = args
                        .next()
                        .and_then(|s| s.parse::<f32>().ok())
                        .map(gain::clamp_db)
                        .unwrap_or(0.0);
                }
            }

            info!("starting trimmer from stdin (rate={sample_rate}, channels={channels}, gain={gain_db} dB)");
            run_trimmer_from_stdin(
                sample_rate,
                channels,
                TrimmerOptions {
                    preview_sink,
                    save_target: SaveTarget {
                        clips_dir,
                        source,
                        mode: save_mode,
                    },
                    initial_selection_secs: initial_selection_secs(
                        selection_start_secs,
                        selection_end_secs,
                    ),
                    gain_db,
                },
            );
        }
        None => print_usage(),
        Some(other) => {
            warn!("unknown command: {other}");
            print_usage();
            std::process::exit(1);
        }
    }
}

fn init_logging() {
    let _ = env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .write_style(env_logger::WriteStyle::Never)
        .format_timestamp_secs()
        .try_init();
}

fn print_usage() {
    eprintln!("Clippying - Audio buffer and trimmer\n");
    eprintln!("Usage:");
    eprintln!("  clippying start          Start the background WebSocket manager daemon (logs to /tmp/clippying.log)");
    eprintln!("  clippying stop           Stop the background daemon");
    eprintln!("  clippying restart        Restart the background daemon");
    eprintln!("\n");
}

fn run_trimmer_from_stdin(sample_rate: u32, channels: u8, options: TrimmerOptions) {
    let mut buf = Vec::new();
    if let Err(e) = std::io::stdin().read_to_end(&mut buf) {
        error!("failed to read stdin: {e}");
        return;
    }

    if buf.is_empty() || buf.len() % 2 != 0 {
        warn!("invalid PCM data on stdin");
        return;
    }

    let samples_i16: Vec<i16> = bytemuck::cast_slice(&buf).to_vec();
    let mono: Vec<i16> = if channels > 1 {
        samples_i16.chunks_exact(channels as usize).map(|f| f[0]).collect()
    } else {
        samples_i16
    };

    run_trimmer_with_clip(Some(AudioClip { samples: mono, sample_rate }), options);
}

fn run_trimmer_with_clip(clip: Option<AudioClip>, options: TrimmerOptions) {
    if let Some(sink) = options
        .preview_sink
        .as_deref()
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
    {
        std::env::set_var("PULSE_SINK", sink);
        info!("using preview sink: {sink}");
    }

    let ui = Trimmer::new().unwrap();
    let state = Rc::new(RefCell::new(TrimmerState::new(
        clip,
        options.save_target,
        options.initial_selection_secs,
        options.gain_db,
    )));

    {
        let s = state.borrow();
        ui.set_gain_db(s.gain.db());
        ui.set_gain_linear(s.gain.linear());
        ui.set_peak_max(s.selection_peak());
        if let Some(clip) = &s.clip {
            ui.set_view_start(0.0);
            ui.set_view_end(1.0);
            ui.set_peaks(compute_peaks_model(&clip.samples));
            ui.set_status(SharedString::from(format!("Loaded ({:.1}s)", clip.duration_secs())));
            let (sel_start, sel_end) = s.selection;
            let has_sel = (sel_end - sel_start).abs() > 0.0001;
            ui.set_sel_start(sel_start);
            ui.set_sel_end(sel_end);
            ui.set_has_selection(has_sel);
            if has_sel {
                let dur = clip.duration_secs();
                let (t0, t1) = (sel_start.min(sel_end) * dur, sel_start.max(sel_end) * dur);
                ui.set_time_display(SharedString::from(format!("{:.2}s → {:.2}s ({:.2}s)", t0, t1, t1 - t0)));
            }
        }
        ui.set_save_label(SharedString::from(match s.save_target.mode {
            SaveMode::TrimToFile => "💾 Save",
            SaveMode::EmitSelection => "✓ Apply Range",
        }));
    }

    let ui_weak = ui.as_weak();
    let state_clone = state.clone();
    ui.on_selection_changed(move |start, end| {
        let mut s = state_clone.borrow_mut();
        s.selection = (start, end);
        let has_sel = (end - start).abs() > 0.0001;

        if let Some(ui) = ui_weak.upgrade() {
            ui.set_has_selection(has_sel);
            ui.set_peak_max(s.selection_peak());
            if let Some(clip) = &s.clip {
                let dur = clip.duration_secs();
                let (t0, t1) = (start.min(end) * dur, start.max(end) * dur);
                ui.set_time_display(SharedString::from(format!("{:.2}s → {:.2}s ({:.2}s)", t0, t1, t1 - t0)));
            }
        }
    });

    let ui_weak = ui.as_weak();
    let state_clone = state.clone();
    ui.on_gain_changed(move |db| {
        let s = state_clone.borrow();
        s.gain.set_db(db);
        if let Some(ui) = ui_weak.upgrade() {
            ui.set_gain_db(s.gain.db());
            ui.set_gain_linear(s.gain.linear());
        }
    });

    let ui_weak = ui.as_weak();
    let state_clone = state.clone();
    ui.on_play_clicked(move || {
        let mut s = state_clone.borrow_mut();
        s.play_selection();
        if let Some(ui) = ui_weak.upgrade() {
            ui.set_is_playing(s.is_playing());
        }
    });

    let ui_weak = ui.as_weak();
    let state_clone = state.clone();
    ui.on_stop_clicked(move || {
        let mut s = state_clone.borrow_mut();
        s.stop_playback();
        if let Some(ui) = ui_weak.upgrade() {
            ui.set_is_playing(false);
            ui.set_playhead_pos(-1.0);
        }
    });

    let ui_weak = ui.as_weak();
    let state_clone = state.clone();
    ui.on_save_clicked(move || {
        let mut s = state_clone.borrow_mut();
        if let Some(msg) = s.save_selection() {
            if let Some(ui) = ui_weak.upgrade() {
                ui.set_status(SharedString::from(msg));
                let _ = ui.hide();
                slint::quit_event_loop().ok();
            }
        }
    });

    let ui_weak = ui.as_weak();
    ui.on_cancel_clicked(move || {
        println!(
            "{}",
            json!({
                "type": "clip_saved",
                "path": "",
                "canceled": true,
            })
        );
        let _ = std::io::stdout().flush();
        if let Some(ui) = ui_weak.upgrade() {
            let _ = ui.hide();
            slint::quit_event_loop().ok();
        }
    });

    let ui_weak = ui.as_weak();
    let state_clone = state.clone();
    ui.on_zoom_changed(move |zoom, offset| {
        let s = state_clone.borrow();
        if let Some(clip) = &s.clip {
            if let Some(ui) = ui_weak.upgrade() {
                let view_width = 1.0 / zoom;
                let view_start = offset;
                let view_end = (offset + view_width).min(1.0);
                let num_bars = (800.0 * zoom).min(2000.0) as usize;
                ui.set_view_start(view_start);
                ui.set_view_end(view_end);
                ui.set_peaks(compute_peaks_for_range(&clip.samples, view_start, view_end, num_bars));
            }
        }
    });

    let ui_weak = ui.as_weak();
    let state_clone = state.clone();
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, std::time::Duration::from_millis(16), move || {
        let (pos, finished) = {
            let s = state_clone.borrow();
            (s.playhead_position(), s.playback_finished())
        };

        let Some(ui) = ui_weak.upgrade() else { return };
        if let Some(pos) = pos {
            ui.set_playhead_pos(pos);
        }

        if finished {
            let mut s = state_clone.borrow_mut();
            s.stop_playback();
            ui.set_is_playing(false);
            ui.set_playhead_pos(-1.0);
        }
    });

    ui.run().unwrap();
}

struct Playback {
    stop_requested: Arc<AtomicBool>,
    finished: Arc<AtomicBool>,
    start: Instant,
    duration: f32,
    sel_start: f32,
    sel_end: f32,
}

struct TrimmerState {
    clip: Option<AudioClip>,
    selection: (f32, f32),
    playback: Option<Playback>,
    save_target: SaveTarget,
    /// Boost shared with the playback thread so slider moves are audible
    /// during a preview.
    gain: Arc<GainCell>,
    /// Peak magnitude (0..1) per bucket over the whole clip.
    envelope: Vec<f32>,
}

impl TrimmerState {
    fn new(
        clip: Option<AudioClip>,
        save_target: SaveTarget,
        initial_selection_secs: Option<(f32, f32)>,
        gain_db: f32,
    ) -> Self {
        let selection = clip
            .as_ref()
            .and_then(|clip| normalize_selection_secs(clip, initial_selection_secs))
            .unwrap_or((0.0, 1.0));
        let envelope = clip
            .as_ref()
            .map(|clip| compute_envelope(&clip.samples, ENVELOPE_BUCKETS))
            .unwrap_or_default();
        Self {
            clip,
            selection,
            playback: None,
            save_target,
            gain: Arc::new(GainCell::new(gain_db)),
            envelope,
        }
    }

    /// Loudest peak (0..1, before boost) inside the current selection, used by
    /// the UI to warn when the boost would clip.
    fn selection_peak(&self) -> f32 {
        if self.envelope.is_empty() {
            return 0.0;
        }
        let (s0, s1) = self.selection;
        let (start, end) = (s0.min(s1).clamp(0.0, 1.0), s0.max(s1).clamp(0.0, 1.0));
        let len = self.envelope.len() as f32;
        let first = (start * len) as usize;
        let last = ((end * len).ceil() as usize).clamp(first + 1, self.envelope.len());
        self.envelope[first.min(self.envelope.len() - 1)..last]
            .iter()
            .copied()
            .fold(0.0f32, f32::max)
    }

    fn play_selection(&mut self) {
        self.stop_playback();

        let Some(clip) = &self.clip else { return };

        let (s0, s1) = self.selection;
        let (start, end) = (s0.min(s1), s0.max(s1));
        let start_idx = (start * clip.samples.len() as f32) as usize;
        let end_idx = ((end * clip.samples.len() as f32).ceil() as usize).min(clip.samples.len());
        if start_idx >= end_idx { return; }

        let samples: Vec<i16> = clip.samples[start_idx..end_idx].to_vec();
        let sample_rate = clip.sample_rate;
        let duration = samples.len() as f32 / sample_rate as f32;

        let stop_requested = Arc::new(AtomicBool::new(false));
        let finished = Arc::new(AtomicBool::new(false));
        let stop_clone = stop_requested.clone();
        let finished_clone = finished.clone();
        let gain = self.gain.clone();

        let sink_name = std::env::var("PULSE_SINK")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());

        thread::spawn(move || {
            let spec = Spec {
                format: Format::S16NE,
                channels: 1,
                rate: sample_rate,
            };

            let pa = Simple::new(
                None,
                "clippying",
                Direction::Playback,
                sink_name.as_deref(),
                "preview",
                &spec,
                None,
                None,
            );

            let pa = match pa {
                Ok(s) => s,
                Err(e) => {
                    error!("PulseAudio playback error: {e}");
                    finished_clone.store(true, Ordering::Relaxed);
                    return;
                }
            };

            // Write in small chunks so Stop can interrupt promptly, and so a
            // boost change lands within a few milliseconds.
            const CHUNK_SAMPLES: usize = 2048;
            let mut scratch = vec![0i16; CHUNK_SAMPLES];
            for chunk in samples.chunks(CHUNK_SAMPLES) {
                if stop_clone.load(Ordering::Relaxed) {
                    break;
                }
                let linear = gain.linear();
                let out = if linear == 1.0 {
                    chunk
                } else {
                    scratch.clear();
                    scratch.extend(chunk.iter().map(|&s| gain::apply(s, linear)));
                    &scratch[..]
                };
                let bytes = bytemuck::cast_slice::<i16, u8>(out);
                if let Err(e) = pa.write(bytes) {
                    warn!("PulseAudio write error: {e}");
                    break;
                }
            }

            // Let PulseAudio play the buffered tail so preview reaches the handle.
            if !stop_clone.load(Ordering::Relaxed) {
                if let Err(e) = pa.drain() {
                    warn!("PulseAudio drain error: {e}");
                }
            }

            finished_clone.store(true, Ordering::Relaxed);
        });

        self.playback = Some(Playback {
            stop_requested,
            finished,
            start: Instant::now(),
            duration,
            sel_start: start,
            sel_end: end,
        });
    }

    fn stop_playback(&mut self) {
        if let Some(p) = self.playback.take() {
            p.stop_requested.store(true, Ordering::Relaxed);
            p.finished.store(true, Ordering::Relaxed);
        }
    }

    fn is_playing(&self) -> bool {
        self.playback
            .as_ref()
            .is_some_and(|p| !p.finished.load(Ordering::Relaxed))
    }

    fn playback_finished(&self) -> bool {
        self.playback
            .as_ref()
            .is_some_and(|p| p.finished.load(Ordering::Relaxed))
    }

    fn playhead_position(&self) -> Option<f32> {
        let p = self.playback.as_ref()?;
        if p.duration <= f32::EPSILON {
            return Some(p.sel_end);
        }

        let elapsed = p.start.elapsed().as_secs_f32();
        let progress = (elapsed / p.duration).clamp(0.0, 1.0);
        Some(p.sel_start + progress * (p.sel_end - p.sel_start))
    }

    fn save_selection(&mut self) -> Option<String> {
        self.stop_playback();

        let Some(clip) = &self.clip else { return None };

        let (s0, s1) = self.selection;
        let (start, end) = (s0.min(s1), s0.max(s1));
        let mut start_idx = (start * clip.samples.len() as f32) as usize;
        let mut end_idx = ((end * clip.samples.len() as f32).ceil() as usize).min(clip.samples.len());
        if start_idx >= end_idx { return None; }

        let duration_secs = clip.duration_secs();
        let start_secs = start * duration_secs;
        let end_secs = end * duration_secs;

        if matches!(self.save_target.mode, SaveMode::EmitSelection) {
            let evt = json!({
                "type": "selection_saved",
                "start": start_secs,
                "end": end_secs,
                "duration": duration_secs,
                "gain_db": self.gain.db(),
            });
            println!("{}", evt);
            let _ = std::io::stdout().flush();
            return Some(format!("Range: {:.2}s → {:.2}s", start_secs, end_secs));
        }

        let linear = self.gain.linear();
        let threshold = 256i16;
        let padding = (clip.sample_rate as f32 * 0.15) as usize;
        let base_start_idx = start_idx;
        let base_end_idx = end_idx;
        let segment = &clip.samples[start_idx..end_idx];
        let threshold = i32::from(threshold);
        let is_audible = |s: i16| sample_magnitude(gain::apply(s, linear)) > threshold;
        if let (Some(first), Some(last)) = (
            segment.iter().position(|&s| is_audible(s)),
            segment.iter().rposition(|&s| is_audible(s)),
        ) {
            start_idx = (base_start_idx + first).saturating_sub(padding);
            end_idx = (base_start_idx + last + 1 + padding).min(base_end_idx);
        }

        let out_dir = clips_dir(self.save_target.clips_dir.as_deref(), self.save_target.source.as_deref());
        if let Err(e) = std::fs::create_dir_all(&out_dir) {
            return Some(format!("Save error: {}", e));
        }
        let ts = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
        let latest_path = out_dir.join("latest.wav");
        let archive_path = out_dir.join(format!("clip_{}.wav", ts));

        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: clip.sample_rate,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };

        match hound::WavWriter::create(&latest_path, spec) {
            Ok(mut w) => {
                for &s in &clip.samples[start_idx..end_idx] {
                    let _ = w.write_sample(gain::apply(s, linear));
                }
                let _ = w.finalize();
                let saved_path = match std::fs::copy(&latest_path, &archive_path) {
                    Ok(_) => archive_path.clone(),
                    Err(e) => {
                        warn!("failed to archive clip copy: {e}");
                        latest_path.clone()
                    }
                };
                let evt = json!({
                    "type": "clip_saved",
                    "path": latest_path.display().to_string(),
                    "saved_path": saved_path.display().to_string(),
                    "gain_db": self.gain.db(),
                });
                println!("{}", evt);
                Some(format!("Saved: {}", latest_path.display()))
            }
            Err(e) => Some(format!("Save error: {}", e)),
        }
    }
}

fn initial_selection_secs(start_secs: Option<f32>, end_secs: Option<f32>) -> Option<(f32, f32)> {
    match (start_secs, end_secs) {
        (Some(start), Some(end)) if end > start => Some((start, end)),
        _ => None,
    }
}

fn normalize_selection_secs(clip: &AudioClip, selection_secs: Option<(f32, f32)>) -> Option<(f32, f32)> {
    let (start_secs, end_secs) = selection_secs?;
    let duration = clip.duration_secs();
    if duration <= 0.0 {
        return None;
    }

    let start = (start_secs / duration).clamp(0.0, 1.0);
    let end = (end_secs / duration).clamp(0.0, 1.0);
    (end > start).then_some((start, end))
}

fn compute_peaks_model(samples: &[i16]) -> ModelRc<f32> {
    compute_peaks_for_range(samples, 0.0, 1.0, 800)
}

fn compute_peaks_for_range(samples: &[i16], view_start: f32, view_end: f32, num_bars: usize) -> ModelRc<f32> {
    if samples.is_empty() {
        return ModelRc::new(VecModel::from(Vec::new()));
    }
    
    let start_idx = ((view_start * samples.len() as f32) as usize).min(samples.len());
    let end_idx = ((view_end * samples.len() as f32) as usize).min(samples.len());
    let visible_samples = &samples[start_idx..end_idx];
    
    if visible_samples.is_empty() {
        return ModelRc::new(VecModel::from(Vec::new()));
    }
    
    let chunk_size = (visible_samples.len() / num_bars).max(1);
    let peaks: Vec<f32> = visible_samples
        .chunks(chunk_size)
        .take(num_bars)
        .map(|chunk| {
            let max = chunk.iter().map(|&s| sample_magnitude(s)).max().unwrap_or(0) as f32 / 32768.0;
            max
        })
        .collect();
    ModelRc::new(VecModel::from(peaks))
}

fn compute_envelope(samples: &[i16], buckets: usize) -> Vec<f32> {
    if samples.is_empty() || buckets == 0 {
        return Vec::new();
    }
    let chunk_size = samples.len().div_ceil(buckets).max(1);
    samples
        .chunks(chunk_size)
        .map(|chunk| chunk.iter().map(|&s| sample_magnitude(s)).max().unwrap_or(0) as f32 / 32768.0)
        .collect()
}

fn sample_magnitude(sample: i16) -> i32 {
    i32::from(sample).abs()
}

fn clips_dir(base_dir: Option<&std::path::Path>, source: Option<&str>) -> PathBuf {
    let base = base_dir
        .map(|path| path.to_path_buf())
        .unwrap_or_else(|| dirs::home_dir().unwrap_or_else(|| PathBuf::from(".")).join("clips"));
    base.join(sanitize_source_for_path(source.unwrap_or("default-source")))
}

fn sanitize_source_for_path(source: &str) -> String {
    let sanitized: String = source
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-') {
                ch
            } else {
                '_'
            }
        })
        .collect();
    let trimmed = sanitized.trim_matches(|ch| ch == '.' || ch == '_').to_string();
    if trimmed.is_empty() {
        "default-source".to_string()
    } else {
        trimmed
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn save_target(dir: &std::path::Path) -> SaveTarget {
        SaveTarget {
            clips_dir: Some(dir.to_path_buf()),
            source: Some("unit-test".to_string()),
            mode: SaveMode::TrimToFile,
        }
    }

    fn saved_samples(dir: &std::path::Path) -> Vec<i16> {
        let path = dir.join("unit-test").join("latest.wav");
        hound::WavReader::open(path)
            .expect("clip should exist")
            .samples::<i16>()
            .map(|s| s.expect("sample"))
            .collect()
    }

    #[test]
    fn save_writes_boosted_samples() {
        let dir = std::env::temp_dir().join("clippying-save-boost-test");
        let _ = std::fs::remove_dir_all(&dir);

        let clip = AudioClip { samples: vec![1000, -1000, 8000, -8000], sample_rate: 48_000 };
        let mut state = TrimmerState::new(Some(clip), save_target(&dir), None, 20.0);
        state.save_selection().expect("save should report a result");

        assert_eq!(saved_samples(&dir), vec![10_000, -10_000, i16::MAX, i16::MIN]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn save_without_boost_is_untouched() {
        let dir = std::env::temp_dir().join("clippying-save-unity-test");
        let _ = std::fs::remove_dir_all(&dir);

        let clip = AudioClip { samples: vec![1000, -1000, 8000, -8000], sample_rate: 48_000 };
        let mut state = TrimmerState::new(Some(clip), save_target(&dir), None, 0.0);
        state.save_selection().expect("save should report a result");

        assert_eq!(saved_samples(&dir), vec![1000, -1000, 8000, -8000]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn boost_makes_quiet_audio_pass_the_silence_trim() {
        // Below the 256 trim threshold at unity, audible once boosted.
        let quiet = vec![0i16, 0, 40, -40, 0, 0];
        let dir = std::env::temp_dir().join("clippying-save-trim-test");
        let _ = std::fs::remove_dir_all(&dir);

        let clip = AudioClip { samples: quiet.clone(), sample_rate: 48_000 };
        let mut state = TrimmerState::new(Some(clip), save_target(&dir), None, 0.0);
        state.save_selection().expect("save should report a result");
        // Nothing crosses the threshold, so the full selection is written.
        assert_eq!(saved_samples(&dir), quiet);
        let _ = std::fs::remove_dir_all(&dir);

        let clip = AudioClip { samples: quiet, sample_rate: 48_000 };
        let mut state = TrimmerState::new(Some(clip), save_target(&dir), None, 20.0);
        state.save_selection().expect("save should report a result");
        assert_eq!(saved_samples(&dir), vec![0, 0, 400, -400, 0, 0]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn selection_peak_tracks_the_selected_range() {
        let mut samples = vec![100i16; 1000];
        samples.extend(std::iter::repeat_n(32_000, 1000));

        let dir = std::env::temp_dir().join("clippying-peak-test");
        let clip = AudioClip { samples, sample_rate: 48_000 };
        let mut state = TrimmerState::new(Some(clip), save_target(&dir), None, 0.0);

        state.selection = (0.0, 0.5);
        assert!(state.selection_peak() < 0.01);

        state.selection = (0.5, 1.0);
        assert!(state.selection_peak() > 0.9);
    }
}
