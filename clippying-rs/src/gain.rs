//! Shared gain helpers.
//!
//! Boost is expressed in dB wherever it crosses a process or protocol boundary
//! (WebSocket commands, trimmer CLI args, UI) and converted to a linear factor
//! at the point where samples are actually scaled.

use std::sync::atomic::{AtomicU32, Ordering};

pub const MIN_GAIN_DB: f32 = -30.0;
pub const MAX_GAIN_DB: f32 = 30.0;

pub fn clamp_db(db: f32) -> f32 {
    if db.is_nan() {
        0.0
    } else {
        db.clamp(MIN_GAIN_DB, MAX_GAIN_DB)
    }
}

pub fn db_to_linear(db: f32) -> f32 {
    let db = clamp_db(db);
    if db == 0.0 {
        1.0
    } else {
        10f32.powf(db / 20.0)
    }
}

/// Scale a sample, hard-clamping instead of wrapping on overflow.
pub fn apply(sample: i16, linear: f32) -> i16 {
    if linear == 1.0 {
        return sample;
    }
    (sample as f32 * linear).clamp(i16::MIN as f32, i16::MAX as f32) as i16
}

/// A gain value that can be read by an audio thread while the UI (or a daemon
/// command) changes it, so boost edits take effect on the next buffer.
pub struct GainCell(AtomicU32);

impl GainCell {
    pub fn new(db: f32) -> Self {
        Self(AtomicU32::new(clamp_db(db).to_bits()))
    }

    pub fn set_db(&self, db: f32) {
        self.0.store(clamp_db(db).to_bits(), Ordering::Relaxed);
    }

    pub fn db(&self) -> f32 {
        f32::from_bits(self.0.load(Ordering::Relaxed))
    }

    pub fn linear(&self) -> f32 {
        db_to_linear(self.db())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unity_and_bounds() {
        assert_eq!(db_to_linear(0.0), 1.0);
        assert_eq!(db_to_linear(20.0), 10.0);
        assert!((db_to_linear(6.0) - 1.9952624).abs() < 1e-5);
        assert_eq!(clamp_db(f32::NAN), 0.0);
        assert_eq!(clamp_db(100.0), MAX_GAIN_DB);
        assert_eq!(clamp_db(-100.0), MIN_GAIN_DB);
    }

    #[test]
    fn apply_clamps_instead_of_wrapping() {
        assert_eq!(apply(1234, 1.0), 1234);
        assert_eq!(apply(1000, 10.0), 10_000);
        assert_eq!(apply(8000, 10.0), i16::MAX);
        assert_eq!(apply(-8000, 10.0), i16::MIN);
    }

    #[test]
    fn cell_round_trips_db() {
        let cell = GainCell::new(0.0);
        cell.set_db(6.0);
        assert_eq!(cell.db(), 6.0);
        assert_eq!(cell.linear(), db_to_linear(6.0));
        cell.set_db(999.0);
        assert_eq!(cell.db(), MAX_GAIN_DB);
    }
}
