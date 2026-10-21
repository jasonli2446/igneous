// Shared by the 3D canvas and the page. Kept free of WebGL imports so it is safe to load during server
// rendering (importing it from Graph3D would drag three.js into SSR and crash with "window is not defined").

export type Community = { color: string; members: string[]; size: number };

// Each category keeps one hue across the graph, so a glance tells you what kind of thing a dot is.
export const CATEGORY_COLOR: Record<string, string> = {
  People: "#6aa6ff", Groups: "#4fd1a5", Events: "#f5b14c", Organizations: "#b48cff",
  Projects: "#ff7aa8", Courses: "#4cc9e6", Code: "#ff8a65", Files: "#a3d65c", Topics: "#e879f9", Places: "#facc15",
};

// Nodes added by /graph/expand carry no `category`; fall back to the category their type belongs to (#96).
export const TYPE_CATEGORY: Record<string, string> = {
  Person: "People", Thread: "Groups", Event: "Events", Organization: "Organizations",
  Topic: "Topics", File: "Files", Folder: "Files", Project: "Projects", Course: "Courses", Repo: "Code", Place: "Places",
};
