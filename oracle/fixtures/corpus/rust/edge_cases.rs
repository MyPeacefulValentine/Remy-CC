//! Edge cases for the Rust fixture: cfg-gated duplicates, nested block
//! comments, grouped use lists, and trait method signatures.

use crate::shapes::{Circle, HasArea as Surface};

#[cfg(unix)]
fn platform_id() -> i64 {
    1
}

#[cfg(windows)]
fn platform_id() -> i64 {
    2
}

/// Doc over attribute.
#[derive(Debug)]
pub enum Mode {
    Fast,
    /* inline /* nested */ comment */ Slow,
}

pub trait Runner {
    fn run(&self) -> i64;
    fn label() -> &'static str {
        "runner"
    }
}

impl Runner for Mode {
    fn run(&self) -> i64 {
        let circle = Circle { radius: 1.0 };
        circle.area() as i64 + platform_id()
    }
}

macro_rules! twice {
    ($x:expr) => {
        $x * 2
    };
}

mod nested_scope {
    pub fn scoped_helper() -> i64 {
        twice!(3)
    }
}

pub type ModeAlias = Mode;

pub fn entry() -> i64 {
    nested_scope::scoped_helper() + std::mem::size_of::<Mode>() as i64
}
