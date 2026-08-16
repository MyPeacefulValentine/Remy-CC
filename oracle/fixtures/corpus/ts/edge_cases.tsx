import { Circle } from "../ts_dup/na";
import type { Shape } from "./shapes";
import { describeShape } from "./shapes";
const legacy = require("./shapes");

/** Renders one card. */
function Card(props: { label: string }): unknown {
  return <div className="card">{props.label}</div>;
}

/// Arrow docs live on the declaration statement.
export const render = (count: number) => {
  const cards = makeCards(count);
  return cards.map(Card);
};

export default class Panel {
  private items: string[] = [];

  /** Adds one entry. */
  push(label: string): void {
    this.items.push(label);
  }
}

module legacyNs {
  export type Marker = string;
}

export namespace visible {
  export const nested = () => describeShape(null as never);
}

function makeCards(count: number): string[] {
  return new Array(count).fill("card");
}
