import { describe, expect, it } from "vitest"

import type { ContentItemStatus } from "./api"
import { statusBadgeClass, statusLabel } from "./content-status"

// Every member of the union, listed by hand. `tsc` guarantees the badge map is total, but only
// against the union *as this build sees it* — this list is what proves the union itself is the
// full backend set at runtime. Backend parity is asserted separately by the Python suite
// (backend/tests/test_dashboard_types.py).
const ALL_STATUSES: ContentItemStatus[] = [
  "generated",
  "critic_passed",
  "critic_failed",
  "guard_failed",
  "rendering",
  "render_failed",
  "scheduled",
  "published",
  "publish_failed",
  "retracted",
]

describe("statusBadgeClass", () => {
  it("gives every known status its own colour, never the muted fallback", () => {
    for (const status of ALL_STATUSES) {
      expect(statusBadgeClass(status), status).not.toContain("bg-muted")
    }
  })

  it("distinguishes rendering from render_failed", () => {
    // S5.2.1 (#83): both used to land in the same muted fallback, so an in-flight render looked
    // identical to a failed one.
    expect(statusBadgeClass("rendering")).not.toBe(statusBadgeClass("render_failed"))
  })

  it("falls back to muted for a status this build has never heard of", () => {
    // The backend can be a deploy ahead of the dashboard; an unknown status must render plainly
    // rather than produce `undefined` in the className.
    const future = "quantum_entangled" as ContentItemStatus
    expect(statusBadgeClass(future)).toBe("bg-muted text-muted-foreground")
  })
})

describe("statusLabel", () => {
  it("turns the wire format into something readable", () => {
    expect(statusLabel("render_failed")).toBe("render failed")
    expect(statusLabel("critic_passed")).toBe("critic passed")
  })

  it("leaves single-word statuses alone", () => {
    expect(statusLabel("published")).toBe("published")
  })
})
