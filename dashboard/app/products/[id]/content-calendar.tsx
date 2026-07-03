import { getContentCalendar, type CalendarItem } from "@/lib/api"

import { CalendarGrid } from "./calendar-grid"

// S6.3: content calendar. Fetches its own data and degrades to an empty grid on fetch failure,
// matching the Funnel section's convention for sections whose endpoint can 404 independently.
export async function ContentCalendar({ productId }: { productId: number }) {
  let items: CalendarItem[] = []
  try {
    items = await getContentCalendar(productId)
  } catch {
    items = []
  }

  return <CalendarGrid items={items} />
}
