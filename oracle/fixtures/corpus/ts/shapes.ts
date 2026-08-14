export interface Shape {
  area(): number;
}

export class Circle implements Shape {
  constructor(private radius: number) {}

  area(): number {
    return 3.14 * this.radius * this.radius;
  }
}

export function describeShape(shape: Shape): number {
  return shape.area();
}
