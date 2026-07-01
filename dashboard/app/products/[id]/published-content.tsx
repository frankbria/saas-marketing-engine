"use client"

import { useRouter } from "next/navigation"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { retractContent, type ContentItem } from "@/lib/api"

// S4.7: list live posts and let the operator retract one — deletes the remote post and flips the
// item to `retracted`. The kill switch (S4.6) only stops future posts; this pulls a bad live one.
export function PublishedContent({
  productId,
  items,
}: {
  productId: number
  items: ContentItem[]
}) {
  const router = useRouter()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run<T>(fn: () => Promise<T>) {
    setBusy(true)
    setError(null)
    try {
      await fn()
      router.refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed")
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-sm font-semibold">Published content</h2>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing published yet.</p>
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
                  {item.status === "retracted" && (
                    <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">
                      retracted
                    </span>
                  )}
                </span>
                {item.external_url && (
                  <a
                    href={item.external_url}
                    target="_blank"
                    rel="noreferrer"
                    className="truncate text-xs text-muted-foreground hover:underline"
                  >
                    {item.external_url}
                  </a>
                )}
              </div>
              {item.status === "published" && (
                <Button
                  type="button"
                  variant="outline"
                  size="xs"
                  disabled={busy}
                  onClick={() => run(() => retractContent(productId, item.id))}
                >
                  Retract
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
