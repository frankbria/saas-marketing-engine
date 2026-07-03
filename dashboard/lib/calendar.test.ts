// Pin a non-UTC zone BEFORE any Date use so local-time parsing bugs surface deterministically
// (CI runs in UTC, where naive-parse bugs are invisible).
process.env.TZ = "America/Los_Angeles"

import { describe, expect, it } from "vitest"

import { type CalendarItem } from "./api"
import { anchorDate, monthGrid, parseUtc } from "./calendar"

function item(overrides: Partial<CalendarItem> = {}): CalendarItem {
  return {
    id: 1,
    channel_id: 1,
    content_type: "post",
    title: "A post",
    status: "generated",
    spot_check: false,
    critic_score: null,
    scheduled_for: null,
    published_at: null,
    created_at: "2026-07-10T12:00:00Z",
    external_url: null,
    metrics: { impressions: 0, visits: 0, signups: 0, paid: 0, revenue_cents: 0 },
    ...overrides,
  }
}

describe("anchorDate", () => {
  it("prefers published_at", () => {
    const it_ = item({
      published_at: "2026-07-02T09:00:00Z",
      scheduled_for: "2026-07-01T09:00:00Z",
    })
    expect(anchorDate(it_)).toBe("2026-07-02T09:00:00Z")
  })

  it("falls back to scheduled_for", () => {
    const it_ = item({ scheduled_for: "2026-07-01T09:00:00Z" })
    expect(anchorDate(it_)).toBe("2026-07-01T09:00:00Z")
  })

  it("falls back to created_at when nothing else is set", () => {
    expect(anchorDate(item())).toBe("2026-07-10T12:00:00Z")
  })
})

describe("parseUtc", () => {
  it("treats offset-less API timestamps as UTC", () => {
    // FastAPI serializes the DB's naive-UTC datetimes with no offset — this is the real wire format.
    expect(parseUtc("2026-07-01T23:30:00").toISOString()).toBe(
      "2026-07-01T23:30:00.000Z"
    )
  })

  it("parses explicit-offset timestamps unchanged", () => {
    expect(parseUtc("2026-07-01T23:30:00Z").toISOString()).toBe(
      "2026-07-01T23:30:00.000Z"
    )
    expect(parseUtc("2026-07-01T23:30:00+02:00").toISOString()).toBe(
      "2026-07-01T21:30:00.000Z"
    )
  })

  it("handles fractional seconds", () => {
    expect(parseUtc("2026-07-01T23:30:00.123456").toISOString()).toBe(
      "2026-07-01T23:30:00.123Z"
    )
  })
})

describe("monthGrid", () => {
  it("builds Monday-start weeks of 7 cells with leading/trailing blanks", () => {
    // July 2026 starts on a Wednesday → 2 leading blanks; 31 days → 2 trailing blanks; 5 weeks.
    const weeks = monthGrid(2026, 7, [])
    expect(weeks).toHaveLength(5)
    for (const week of weeks) expect(week).toHaveLength(7)
    expect(weeks[0].slice(0, 2).map((c) => c.day)).toEqual([null, null])
    expect(weeks[0][2].day).toBe(1)
    expect(weeks[4][4].day).toBe(31)
    expect(weeks[4].slice(5).map((c) => c.day)).toEqual([null, null])
  })

  it("handles a month starting on Monday with no leading blanks", () => {
    // June 2026 starts on a Monday.
    const weeks = monthGrid(2026, 6, [])
    expect(weeks[0][0].day).toBe(1)
  })

  it("leaves every day empty for an empty month", () => {
    const weeks = monthGrid(2026, 7, [])
    for (const cell of weeks.flat()) expect(cell.items).toEqual([])
  })

  it("buckets items onto their anchor day", () => {
    const a = item({ id: 1, published_at: "2026-07-15T10:00:00Z" })
    const weeks = monthGrid(2026, 7, [a])
    const day15 = weeks.flat().find((c) => c.day === 15)
    expect(day15?.items).toEqual([a])
  })

  it("uses UTC date parts so late-evening timestamps stay on their UTC day", () => {
    const a = item({ id: 1, published_at: "2026-07-15T23:30:00Z" })
    const day15 = monthGrid(2026, 7, [a])
      .flat()
      .find((c) => c.day === 15)
    expect(day15?.items).toHaveLength(1)
  })

  it("buckets offset-less wire-format timestamps on their UTC day", () => {
    // A local-time parse in America/Los_Angeles would land this on July 16.
    const a = item({ id: 1, published_at: "2026-07-15T23:30:00" })
    const day15 = monthGrid(2026, 7, [a])
      .flat()
      .find((c) => c.day === 15)
    expect(day15?.items).toHaveLength(1)
  })

  it("keeps early-UTC-morning items in the month for west-of-UTC viewers", () => {
    // A local-time parse in America/Los_Angeles would shift this to June 30 and drop it from July.
    const a = item({ id: 1, published_at: "2026-07-01T02:00:00" })
    const day1 = monthGrid(2026, 7, [a])
      .flat()
      .find((c) => c.day === 1)
    expect(day1?.items).toHaveLength(1)
  })

  it("keeps multiple items on the same day in input order", () => {
    const a = item({ id: 2, published_at: "2026-07-15T18:00:00Z" })
    const b = item({ id: 1, published_at: "2026-07-15T09:00:00Z" })
    const day15 = monthGrid(2026, 7, [a, b])
      .flat()
      .find((c) => c.day === 15)
    expect(day15?.items.map((i) => i.id)).toEqual([2, 1])
  })

  it("excludes items anchored outside the month", () => {
    const june = item({ id: 1, published_at: "2026-06-30T10:00:00Z" })
    const august = item({ id: 2, published_at: "2026-08-01T10:00:00Z" })
    const cells = monthGrid(2026, 7, [june, august]).flat()
    expect(cells.every((c) => c.items.length === 0)).toBe(true)
  })

  it("covers February in a non-leap year", () => {
    // Feb 2026 starts on a Sunday → 6 leading blanks; 28 days.
    const weeks = monthGrid(2026, 2, [item({ published_at: "2026-02-28T10:00:00Z" })])
    expect(weeks[0].slice(0, 6).map((c) => c.day)).toEqual([
      null,
      null,
      null,
      null,
      null,
      null,
    ])
    const days = weeks.flat().filter((c) => c.day !== null)
    expect(days).toHaveLength(28)
    expect(days[27].items).toHaveLength(1)
  })
})
