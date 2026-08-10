import type { ContentItemStatus } from "@/lib/api"

// A *total* Record, not Partial: adding a status to `ContentItemStatus` without giving it a
// colour then fails `tsc` instead of silently landing in a muted fallback — which is how
// `rendering` and `render_failed` went unstyled for two phases (S5.2.1, #83).
const STATUS_BADGE: Record<ContentItemStatus, string> = {
  generated: "bg-sky-100 text-sky-800",
  critic_passed: "bg-teal-100 text-teal-800",
  critic_failed: "bg-red-100 text-red-800",
  guard_failed: "bg-red-100 text-red-800",
  // S5.1: media render in flight on the GPU queue — in progress, not a problem.
  rendering: "bg-violet-100 text-violet-800",
  render_failed: "bg-red-100 text-red-800",
  scheduled: "bg-blue-100 text-blue-800",
  published: "bg-green-100 text-green-800",
  publish_failed: "bg-red-100 text-red-800",
  retracted: "bg-amber-100 text-amber-800",
}

const MUTED = "bg-muted text-muted-foreground"

/** Badge classes for a content status. Falls back to muted for a status this build has never
 * heard of — the backend can be a deploy ahead of the dashboard, and an unknown status should
 * render plainly rather than crash the calendar. */
export function statusBadgeClass(status: ContentItemStatus): string {
  return STATUS_BADGE[status] ?? MUTED
}

/** `render_failed` → `render failed`. Underscores are a wire format, not a label. */
export function statusLabel(status: ContentItemStatus): string {
  return status.replace(/_/g, " ")
}
