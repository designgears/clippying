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

mod daemon;

struct AudioClip {
    samples: Vec<i16>,
    sample_rate: u32,
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
            while let Some(a) = args.next() {
                if a == "--preview-sink" {
                    preview_sink = args.next();
                }
            }

            info!("starting trimmer from stdin (rate={sample_rate}, channels={channels})");
            run_trimmer_from_stdin(sample_rate, channels, preview_sink);
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

fn run_trimmer_from_stdin(sample_rate: u32, channels: u8, preview_sink: Option<String>) {
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

    run_trimmer_with_clip(Some(AudioClip { samples: mono, sample_rate }), preview_sink);
}

fn run_trimmer_with_clip(clip: Option<AudioClip>, preview_sink: Option<String>) {
    if let Some(sink) = preview_sink.as_deref().map(|s| s.trim()).filter(|s| !s.is_empty()) {
        std::env::set_var("PULSE_SINK", sink);
        info!("using preview sink: {sink}");
    }

    let ui = Trimmer::new().unwrap();
    let state = Rc::new(RefCell::new(TrimmerState::new(clip)));

    {
        let s = state.borrow();
        if let Some(clip) = &s.clip {
            ui.set_view_start(0.0);
            ui.set_view_end(1.0);
            ui.set_peaks(compute_peaks_model(&clip.samples));
            ui.set_status(SharedString::from(format!("Loaded ({:.1}s)", clip.duration_secs())));
        }
    }

    let ui_weak = ui.as_weak();
    let state_clone = state.clone();
    ui.on_selection_changed(move |start, end| {
        let mut s = state_clone.borrow_mut();
        s.selection = (start, end);
        let has_sel = (end - start).abs() > 0.0001;

        if let Some(ui) = ui_weak.upgrade() {
            ui.set_has_selection(has_sel);
            if let Some(clip) = &s.clip {
                let dur = clip.duration_secs();
                let (t0, t1) = (start.min(end) * dur, start.max(end) * dur);
                ui.set_time_display(SharedString::from(format!("{:.2}s → {:.2}s ({:.2}s)", t0, t1, t1 - t0)));
            }
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
            let pos = s.playhead_position();
            (pos, s.playback.is_some() && pos.is_none())
        };

        let Some(ui) = ui_weak.upgrade() else { return };
        if let Some(pos) = pos {
            ui.set_playhead_pos(pos);
            return;
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
}

impl TrimmerState {
    fn new(clip: Option<AudioClip>) -> Self {
        Self {
            clip,
            selection: (0.0, 0.0),
            playback: None,
        }
    }

    fn play_selection(&mut self) {
        self.stop_playback();

        let Some(clip) = &self.clip else { return };

        let (s0, s1) = self.selection;
        let (start, end) = (s0.min(s1), s0.max(s1));
        let start_idx = (start * clip.samples.len() as f32) as usize;
        let end_idx = (end * clip.samples.len() as f32) as usize;
        if start_idx >= end_idx { return; }

        let samples: Vec<i16> = clip.samples[start_idx..end_idx].to_vec();
        let sample_rate = clip.sample_rate;
        let duration = samples.len() as f32 / sample_rate as f32;

        let stop_requested = Arc::new(AtomicBool::new(false));
        let finished = Arc::new(AtomicBool::new(false));
        let stop_clone = stop_requested.clone();
        let finished_clone = finished.clone();

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

            // Write in small chunks so Stop can interrupt promptly.
            const CHUNK_SAMPLES: usize = 2048;
            for chunk in samples.chunks(CHUNK_SAMPLES) {
                if stop_clone.load(Ordering::Relaxed) {
                    break;
                }
                let bytes = bytemuck::cast_slice::<i16, u8>(chunk);
                if let Err(e) = pa.write(bytes) {
                    warn!("PulseAudio write error: {e}");
                    break;
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

    fn playhead_position(&self) -> Option<f32> {
        let p = self.playback.as_ref()?;
        if p.finished.load(Ordering::Relaxed) {
            return None;
        }
        let elapsed = p.start.elapsed().as_secs_f32();
        (elapsed < p.duration).then(|| {
            let progress = elapsed / p.duration;
            p.sel_start + progress * (p.sel_end - p.sel_start)
        })
    }

    fn save_selection(&mut self) -> Option<String> {
        self.stop_playback();

        let Some(clip) = &self.clip else { return None };

        let (s0, s1) = self.selection;
        let (start, end) = (s0.min(s1), s0.max(s1));
        let mut start_idx = (start * clip.samples.len() as f32) as usize;
        let mut end_idx = (end * clip.samples.len() as f32) as usize;
        if start_idx >= end_idx { return None; }

        let threshold = 256i16;
        let padding = (clip.sample_rate as f32 * 0.15) as usize;
        let base_start_idx = start_idx;
        let base_end_idx = end_idx;
        let segment = &clip.samples[start_idx..end_idx];
        if let (Some(first), Some(last)) = (
            segment.iter().position(|&s| s.abs() > threshold),
            segment.iter().rposition(|&s| s.abs() > threshold),
        ) {
            start_idx = (base_start_idx + first).saturating_sub(padding);
            end_idx = (base_start_idx + last + 1 + padding).min(base_end_idx);
        }

        let out_dir = clips_dir();
        if let Err(e) = std::fs::create_dir_all(&out_dir) {
            return Some(format!("Save error: {}", e));
        }
        let ts = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
        let trimmed_path = out_dir.join(format!("clip_{}.wav", ts));

        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: clip.sample_rate,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };

        match hound::WavWriter::create(&trimmed_path, spec) {
            Ok(mut w) => {
                for &s in &clip.samples[start_idx..end_idx] {
                    let _ = w.write_sample(s);
                }
                let _ = w.finalize();
                let evt = json!({
                    "type": "clip_saved",
                    "path": trimmed_path.display().to_string(),
                });
                println!("{}", evt);
                Some(format!("Saved: {}", trimmed_path.display()))
            }
            Err(e) => Some(format!("Save error: {}", e)),
        }
    }
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
            let max = chunk.iter().map(|s| s.abs()).max().unwrap_or(0) as f32 / 32768.0;
            max
        })
        .collect();
    ModelRc::new(VecModel::from(peaks))
}

fn clips_dir() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from(".")).join("clips")
}
