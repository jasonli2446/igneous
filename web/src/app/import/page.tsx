import { redirect } from "next/navigation";

// Exports are imported on the Sources page; this path sends old links there.
export default function ImportPage() {
  redirect("/settings");
}
