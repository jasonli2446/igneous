import type { Metadata } from "next";
import Explorer from "@/components/Explorer";

// The root layout's title template only reaches child segments, so this page, in the root segment, names itself in full.
export const metadata: Metadata = { title: "Graph · Igneous" };

export default function GraphPage() {
  return <Explorer />;
}
