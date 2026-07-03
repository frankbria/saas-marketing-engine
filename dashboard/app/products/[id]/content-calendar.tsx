import { getContentCalendar, type CalendarItem } from "@/lib/api"

import { CalendarGrid } from "./calendar-grid"

// S6.3: content calendar. Fetches its own data (like Funnel) since the calendar endpoint 404s
// independently of the rest of the page until the backend lane has landed; degrade to empty.
export async function ContentCalendar({ productId }: { productId: number }) {
  let items: CalendarItem[] = []
  try {
    items = await getContentCalendar(productId)
  } catch {
    items = []
  }

  return <CalendarGrid items={items} />
}
