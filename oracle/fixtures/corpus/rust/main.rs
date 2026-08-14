mod shapes;

fn main() {
    let circle = shapes::Circle { radius: 2.0 };
    let area = shapes::area_of(&circle);
    println!("{area}");
}
