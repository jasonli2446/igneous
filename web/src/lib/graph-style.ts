// Shared by the 3D canvas and the page. Kept free of WebGL imports so it is safe to load during server
// rendering (importing it from Graph3D would drag three.js into SSR and crash with "window is not defined").

export type Community = { color: string; members: string[]; size: number };

// Each category keeps one hue across the graph, so a glance tells you what kind of thing a dot is.
export const CATEGORY_COLOR: Record<string, string> = {
  People: "#6aa6ff", Groups: "#4fd1a5", Events: "#f5b14c", Organizations: "#b48cff",
  Projects: "#ff7aa8", Courses: "#4cc9e6", Code: "#ff8a65", Files: "#a3d65c", Topics: "#e879f9", Places: "#facc15",
  Trips: "#38bdf8", Work: "#f97316", Hobbies: "#84cc16",
};

/** A section's color: a fixed one for known sections, otherwise a stable hue from its name (sections come from
 *  the data, so a new one like "Clubs" still gets a consistent, distinct color). */
export function categoryColor(name: string | null | undefined): string {
  if (!name) return "#a8aca3";
  if (CATEGORY_COLOR[name]) return CATEGORY_COLOR[name];
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360} 65% 60%)`;
}

// Nodes added by /graph/expand carry no `category`; fall back to the category their type belongs to (#96).
export const TYPE_CATEGORY: Record<string, string> = {
  Person: "People", Thread: "Groups", Event: "Events", Organization: "Organizations",
  Topic: "Topics", File: "Files", Folder: "Files", Project: "Projects", Course: "Courses", Repo: "Code", Place: "Places",
};

/** What to call a node's type in the UI (the stored type names are internal). */
export const TYPE_LABEL: Record<string, string> = {
  Person: "Person", Thread: "Conversation", Message: "Message", Event: "Event", Organization: "Organization",
  Project: "Project", Place: "Place", File: "File", Folder: "Folder", Repo: "Repository", Course: "Course",
  Topic: "Topic", Commitment: "Commitment", Decision: "Decision",
};

export function typeLabel(type: string, props?: { group?: boolean } | null) {
  if (type === "Thread") return props?.group ? "Group chat" : "Conversation";
  return TYPE_LABEL[type] ?? type;
}
