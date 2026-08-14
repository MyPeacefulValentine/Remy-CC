import { Circle, describeShape } from "./shapes";

export function run(): number {
  const circle = new Circle(2);
  return describeShape(circle);
}
