//! Geometry primitives for the oracle Rust fixture.

/// A circle described by its radius.
pub struct Circle {
    pub radius: f64,
}

/// Anything with a measurable surface.
pub trait HasArea {
    fn area(&self) -> f64;
}

impl HasArea for Circle {
    fn area(&self) -> f64 {
        self.radius * self.radius * 3.14
    }
}

/// Shape families the fixture distinguishes.
pub enum Kind {
    Round,
    Flat,
}

pub type Radius = f64;

macro_rules! double {
    ($x:expr) => {
        $x * 2.0
    };
}

#[cfg(windows)]
pub fn platform_tag() -> &'static str {
    "win"
}

#[cfg(unix)]
pub fn platform_tag() -> &'static str {
    "unix"
}

/// Compute the area of a circle, exercising a macro and a trait method.
pub fn area_of(shape: &Circle) -> f64 {
    let _doubled = double!(shape.radius);
    shape.area()
}
