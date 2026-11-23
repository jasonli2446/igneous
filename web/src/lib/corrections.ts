// A correction made in a panel, as the Graph page takes it: what changed in words, the API's answer (when it applies
// and the node pairs it touched), how to undo it, and the node to stay on when it moved one (a merge).

import type { Settled } from "@/lib/api";

export type Corrected = {
  text: string;
  answer: Settled;
  undo?: () => Promise<Settled>;
  keep?: number;
};

/** One key for a pair of nodes, whichever way round. */
export const pairKey = (a: number, b: number) => (a < b ? `${a}-${b}` : `${b}-${a}`);
