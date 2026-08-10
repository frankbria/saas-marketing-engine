import { describe, expect, it } from "vitest"

import { cn, formatReach } from "./utils"

describe("cn", () => {
  it("merges class names", () => {
    expect(cn("a", "b")).toBe("a b")
  })

  it("dedupes conflicting tailwind classes (last wins)", () => {
    expect(cn("p-2", "p-4")).toBe("p-4")
  })

  it("drops falsy values", () => {
    expect(cn("a", false && "b", undefined, "c")).toBe("a c")
  })
})

describe("formatReach", () => {
  it("formats a measured count", () => {
    expect(formatReach(1234)).toBe("1,234")
  })

  it("renders a real zero as 0 — that is the shadowban signal, not missing data", () => {
    expect(formatReach(0)).toBe("0")
  })

  it("renders null as an em dash: unmeasured (owned channel), not unseen", () => {
    expect(formatReach(null)).toBe("\u2014")
  })
})
