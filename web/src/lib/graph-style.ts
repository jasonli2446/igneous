// Every color the app draws with outside the stylesheet: section hues for nodes, and the theme's colors for the 3D
// graph. Kept free of WebGL imports so it is safe to load during server rendering (importing it from Graph3D would
// drag three.js into SSR and crash with "window is not defined").

// Each section keeps one hue across the graph and the panels, so a glance tells you what kind of thing a dot is.
const SECTION_HUE: Record<string, string> = {
  People: "#6aa6ff", Groups: "#4fd1a5", Events: "#f5b14c", Organizations: "#b48cff",
  Projects: "#ff7aa8", Courses: "#4cc9e6", Code: "#ff8a65", Files: "#a3d65c", Topics: "#e879f9", Places: "#facc15",
  Trips: "#38bdf8", Work: "#f97316", Hobbies: "#84cc16",
};

/** A section's hue: a fixed one for known sections, otherwise a stable hue from its name (sections come from the data,
 *  so a new one like "Clubs" still gets a consistent, distinct color). Comma syntax, which three.js also parses. */
export function sectionHue(section: string | null | undefined): string | undefined {
  if (!section) return undefined;
  if (SECTION_HUE[section]) return SECTION_HUE[section];
  let h = 0;
  for (const ch of section) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360}, 65%, 60%)`;
}

// The section a node's type belongs to, for nodes that arrive without one (a search result, a node opened from the
// panel, a summary's highlight).
const TYPE_SECTION: Record<string, string> = {
  Person: "People", Thread: "Groups", Event: "Events", Organization: "Organizations",
  Topic: "Topics", File: "Files", Folder: "Files", Project: "Projects", Course: "Courses", Repo: "Code", Place: "Places",
};

export function sectionOf(node: { type: string; category?: string | null }): string | undefined {
  return node.category ?? TYPE_SECTION[node.type];
}

/** A node's dot in the page: its section's hue, or the theme's muted grey when it has no section. */
export function nodeColor(node: { type: string; category?: string | null }): string {
  return sectionHue(sectionOf(node)) ?? "var(--muted)";
}

/** The theme's colors as globals.css defines them, for drawing outside CSS (the WebGL graph). Read again whenever
 *  the theme changes. */
export function themeColors() {
  const css = getComputedStyle(document.documentElement);
  const token = (name: string) => css.getPropertyValue(name).trim();
  return {
    bg: token("--bg"), ink: token("--ink"), muted: token("--muted"), line: token("--line"), accent: token("--accent"),
    spoke: token("--graph-spoke"), structure: token("--graph-structure"), tie: token("--graph-tie"),
    flow: token("--graph-flow"),
  };
}
export type ThemeColors = ReturnType<typeof themeColors>;

/** What to call a node's type in the UI (the stored type names are internal). */
const TYPE_LABEL: Record<string, string> = {
  Person: "Person", Thread: "Conversation", Message: "Message", Event: "Event", Organization: "Organization",
  Project: "Project", Place: "Place", File: "File", Folder: "Folder", Repo: "Repository", Course: "Course",
  Topic: "Topic", Commitment: "Commitment", Decision: "Decision",
};

/** A node's type in words; a conversation the thread flags mark as a group (`props.group`) is a group chat. */
export function typeLabel(type: string, props?: Record<string, unknown>) {
  if (type === "Thread") return props?.group === true ? "Group chat" : "Conversation";
  return TYPE_LABEL[type] ?? type;
}

/** What a link of each type says, read from its source to its destination ("Ana is part of Atlas"). */
const RELATION: Record<string, string> = {
  CORRESPONDS: "corresponds with", PART_OF: "is part of", INVOLVED_IN: "is involved in", RELATED_TO: "is related to",
  ATTENDED: "attended", MENTIONS: "mentions", ABOUT: "is about", INTRODUCED: "introduced", MEMBER_OF: "is a member of",
  WORKED_AT: "worked at", SENT: "sent", RECEIVED: "received", IN_THREAD: "is in", REPLIES_TO: "replied to",
  AUTHORED: "authored", SAME_AS: "is the same as",
};

export function relationLabel(type: string) {
  return RELATION[type] ?? type.toLowerCase().replaceAll("_", " ");
}
