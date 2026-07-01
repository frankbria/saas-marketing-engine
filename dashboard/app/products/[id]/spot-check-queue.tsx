import { type ContentItem } from "@/lib/api"

// S4.9: async spot-check review queue. Surfaces items the engine flagged (first per channel +
// random 10%) so the operator can eyeball a sample. Reviewing is optional/async — this never
// blocks publishing, so the queue is display-only (no approve/reject action).
export function SpotCheckQueue({ items }: { items: ContentItem[] }) {
  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-sm font-semibold">Spot-check queue</h2>

      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Nothing flagged for review. Reviewing is optional and never blocks publishing.
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((item) => (
            <li
              key={item.id}
              className="flex items-center justify-between gap-2 rounded-md border p-3 text-sm"
            >
              <div className="flex min-w-0 flex-col">
                <span className="truncate font-medium">
                  {item.title || item.body.slice(0, 60)}
                </span>
                <span className="text-xs text-muted-foreground">
                  {item.content_type} · {item.status}
                </span>
              </div>
              {item.external_url && (
                <a
                  href={item.external_url}
                  target="_blank"
                  rel="noreferrer"
                  className="shrink-0 text-xs text-muted-foreground hover:underline"
                >
                  View
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
