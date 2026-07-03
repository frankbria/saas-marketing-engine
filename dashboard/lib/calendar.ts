// S6.3: pure date-bucketing helpers for the content calendar. All bucketing uses UTC date parts
// so a timestamp never drifts across days with the viewer's timezone.

import { type CalendarItem } from "./api"

// The date a content item lives on in the calendar: when it went live, else when it's due to,
// else when it was generated.
export function anchorDate(item: CalendarItem): string {
  return item.published_at ?? item.scheduled_for ?? item.created_at
}

export interface DayCell {
  // 1-based day of month; null for the leading/trailing blanks that pad partial weeks.
  day: number | null
  items: CalendarItem[]
}

export type Week = DayCell[]

// Monday-start month grid: weeks of 7 cells, real days carrying that UTC day's items.
// `month` is 1-based (1 = January).
export function monthGrid(
  year: number,
  month: number,
  items: CalendarItem[]
): Week[] {
  const byDay = new Map<number, CalendarItem[]>()
  for (const item of items) {
    const d = new Date(anchorDate(item))
    if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1) continue
    const day = d.getUTCDate()
    byDay.set(day, [...(byDay.get(day) ?? []), item])
  }

  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate()
  // getUTCDay: 0 = Sunday … 6 = Saturday → Monday-start offset.
  const leading = (new Date(Date.UTC(year, month - 1, 1)).getUTCDay() + 6) % 7

  const cells: DayCell[] = Array.from({ length: leading }, () => ({
    day: null,
    items: [],
  }))
  for (let day = 1; day <= daysInMonth; day++) {
    cells.push({ day, items: byDay.get(day) ?? [] })
  }
  while (cells.length % 7 !== 0) cells.push({ day: null, items: [] })

  const weeks: Week[] = []
  for (let i = 0; i < cells.length; i += 7) {
    weeks.push(cells.slice(i, i + 7))
  }
  return weeks
}
