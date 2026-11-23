// A correction made in a panel, as the Graph page takes it: what changed in words, the API's answer (when it applies
// and the node pairs it touched), how to undo it, the node to stay on when it moved one (a merge), and a link you
// added that it takes away or brings back.

import type { Settled } from "@/lib/api";

/** A link you added, by its key (`linkKey`), drawn again (`shown`) or taken away once the correction applies. */
export type LinkChange = { key: string; shown: boolean };

export type Corrected = {
  text: string;
  answer: Settled;
  undo?: () => Promise<Settled>;
  keep?: number;
  link?: LinkChange;
};

/** One key for a pair of nodes, whichever way round. */
export const pairKey = (a: number, b: number) => (a < b ? `${a}-${b}` : `${b}-${a}`);

/** One key for a link you added: its ends, in its direction, and its type. */
export const linkKey = (src: number, dst: number, type: string) => `l${src}-${dst}-${type}`;
