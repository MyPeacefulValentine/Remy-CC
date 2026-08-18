//! Cross-file trait impls for the oracle Rust fixture.

use crate::shapes::HasArea;
use crate::shapes::Kind;

/// Cross-file impl: both the trait and the type live in shapes.rs, so the
/// bases merge and the trait-impl edge require the global postprocess.
impl HasArea for Kind {
    fn area(&self) -> f64 {
        0.0
    }
}

/// One of two same-short-name gauges (the other lives in widgets.rs).
pub struct Gauge;
