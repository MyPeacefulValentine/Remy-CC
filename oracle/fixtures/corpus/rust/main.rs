mod impls;
mod shapes;
mod widgets;

fn main() {
    let circle = shapes::Circle { radius: 2.0 };
    let area = shapes::area_of(&circle);
    println!("{area}");
}

/// Ambiguous impl target: `Gauge` is defined in both impls.rs and
/// widgets.rs and nowhere in this file, so the bases overwrite merges
/// nothing while the trait-impl edge still anchors at this impl site.
impl shapes::HasArea for Gauge {
    fn area(&self) -> f64 {
        2.0
    }
}
