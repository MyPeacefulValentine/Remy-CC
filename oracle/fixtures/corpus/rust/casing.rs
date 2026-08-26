// Case-sensitivity fixture: `use super::Casing` names a type, not a
// module; on case-insensitive filesystems the `Casing.rs` probe must not
// match this file itself, so imports stay empty on every platform.

pub struct Casing;

mod nested {
    use super::Casing;

    pub fn touch(_c: &Casing) {}
}
