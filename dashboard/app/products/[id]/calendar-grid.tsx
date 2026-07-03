"use client"

import { useState } from "react"

import { HugeiconsIcon } from "@hugeicons/react"
import {
  ArrowLeft01Icon,
  ArrowRight01Icon,
  EyeIcon,
} from "@hugeicons/core-free-icons"

import { Button } from "@/components/ui/button"
import { type CalendarItem, type ContentItemStatus } from "@/lib/api"
import { monthGrid } from "@/lib/calendar"

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

// First-class statuses get their own hue; everything in-between stays muted.
const STATUS_BADGE: Partial<Record<ContentItemStatus, string>> = {
  generated: "bg-sky-100 text-sky-800",
  critic_passed: "bg-teal-100 text-teal-800",
  published: "bg-green-100 text-green-800",
  retracted: "bg-amber-100 text-amber-800",
}

export interface CalendarMonth {
  year: number
  month: number // 1-based
}

function currentUtcMonth(): CalendarMonth {
  const now = new Date()
  return { year: now.getUTCFullYear(), month: now.getUTCMonth() + 1 }
}

// S6.3: month grid of content items on their anchor day (published → scheduled → created), with
// status badges, spot-check markers, and compact per-item performance. Client-side month paging
// only — the full item list is already in props.
export function CalendarGrid({
  items,
  initialMonth,
}: {
  items: CalendarItem[]
  initialMonth?: CalendarMonth
}) {
  const [{ year, month }, setMonth] = useState<CalendarMonth>(
    () => initialMonth ?? currentUtcMonth()
  )

  const weeks = monthGrid(year, month, items)
  const monthLabel = new Date(Date.UTC(year, month - 1, 1)).toLocaleString(
    "en-US",
    { month: "long", year: "numeric", timeZone: "UTC" }
  )

  return (
    <section className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">Content calendar</h2>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="icon-xs"
            aria-label="Previous month"
            onClick={() =>
              setMonth(
                month === 1
                  ? { year: year - 1, month: 12 }
                  : { year, month: month - 1 }
              )
            }
          >
            <HugeiconsIcon icon={ArrowLeft01Icon} />
          </Button>
          <span className="w-32 text-center text-sm text-muted-foreground">
            {monthLabel}
          </span>
          <Button
            type="button"
            variant="outline"
            size="icon-xs"
            aria-label="Next month"
            onClick={() =>
              setMonth(
                month === 12
                  ? { year: year + 1, month: 1 }
                  : { year, month: month + 1 }
              )
            }
          >
            <HugeiconsIcon icon={ArrowRight01Icon} />
          </Button>
        </div>
      </div>

      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No content yet. Items appear here once the engine starts generating.
        </p>
      ) : (
        <div className="overflow-hidden rounded-md border">
          <div className="grid grid-cols-7 border-b bg-muted/50 text-center text-xs text-muted-foreground">
            {WEEKDAYS.map((d) => (
              <div key={d} className="py-1">
                {d}
              </div>
            ))}
          </div>
          {weeks.map((week, wi) => (
            <div
              key={wi}
              className={`grid grid-cols-7 ${wi === weeks.length - 1 ? "" : "border-b"}`}
            >
              {week.map((cell, ci) => (
                <div
                  key={ci}
                  className={`flex min-h-20 flex-col gap-1 p-1 ${ci === 6 ? "" : "border-r"} ${cell.day === null ? "bg-muted/30" : ""}`}
                >
                  {cell.day !== null && (
                    <span className="text-xs text-muted-foreground">
                      {cell.day}
                    </span>
                  )}
                  {cell.items.map((item) => (
                    <ItemChip key={item.id} item={item} />
                  ))}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function ItemChip({ item }: { item: CalendarItem }) {
  const hasPerformance =
    item.metrics.impressions > 0 || item.metrics.revenue_cents > 0

  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-md border p-1 text-xs">
      <span className="flex items-center gap-1">
        {item.spot_check && (
          <HugeiconsIcon
            icon={EyeIcon}
            className="size-3 shrink-0 text-amber-600"
            aria-label="Flagged for spot-check"
          />
        )}
        <span className="truncate font-medium">
          {item.title || item.content_type}
        </span>
      </span>
      <span
        className={`self-start truncate rounded px-2 py-0.5 text-xs ${STATUS_BADGE[item.status] ?? "bg-muted text-muted-foreground"}`}
      >
        {item.status}
      </span>
      {hasPerformance && (
        <span className="truncate text-muted-foreground">
          {item.metrics.impressions.toLocaleString()} impr ·{" "}
          {formatCents(item.metrics.revenue_cents)}
        </span>
      )}
    </div>
  )
}

function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`
}
