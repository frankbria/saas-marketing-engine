"use client"

import { useRouter } from "next/navigation"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  goLive,
  setQaItemStatus,
  triggerQaChecklist,
  type LifecycleState,
  type QaChecklistItem,
  type QaItemStatus,
} from "@/lib/api"

// S3.2: the human QA gate. The tester marks each generated item pass/fail with a comment; go-live is
// blocked until every *blocking* item passes, then crosses qa → live. Actionable only while in `qa`
// (before it the checklist is empty; after it the product has already gone live).
export function QaChecklist({
  productId,
  lifecycleState,
  items,
}: {
  productId: number
  lifecycleState: LifecycleState
  items: QaChecklistItem[]
}) {
  const router = useRouter()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Local comment edits keyed by item id; falls back to the persisted comment.
  const [comments, setComments] = useState<Record<number, string>>({})

  const atGate = lifecycleState === "qa"
  // Go-live is blocked until every blocking item passes (mirrors the backend gate so the button
  // reflects the same rule rather than relying on the 409).
  const blockingUnpassed = items.filter((i) => i.blocking && i.status !== "pass")
  const canGoLive = atGate && items.length > 0 && blockingUnpassed.length === 0

  async function mark(item: QaChecklistItem, status: QaItemStatus) {
    setBusy(true)
    setError(null)
    try {
      await setQaItemStatus(productId, item.id, status, comments[item.id] ?? item.comment ?? "")
      router.refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed")
    } finally {
      setBusy(false)
    }
  }

  async function generate() {
    setBusy(true)
    setError(null)
    try {
      await triggerQaChecklist(productId)
      // Generation is async (job queue); refresh now and the rows appear once the worker runs.
      router.refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed")
    } finally {
      setBusy(false)
    }
  }

  async function launch() {
    setBusy(true)
    setError(null)
    try {
      await goLive(productId)
      router.refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed")
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">QA checklist</h2>
        {atGate && (
          <Button type="button" disabled={busy || !canGoLive} onClick={launch}>
            Go live
          </Button>
        )}
      </div>

      {items.length === 0 &&
        (atGate ? (
          <div className="flex flex-col items-start gap-2">
            <p className="text-sm text-muted-foreground">
              No QA checklist yet. Generate it, then mark each step pass/fail.
            </p>
            <Button type="button" variant="outline" disabled={busy} onClick={generate}>
              Generate QA checklist
            </Button>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            No QA checklist yet. Generated at the <span className="font-mono">qa</span> gate.
          </p>
        ))}

      {atGate && items.length > 0 && blockingUnpassed.length > 0 && (
        <p className="text-sm text-muted-foreground">
          {blockingUnpassed.length} blocking item(s) must pass before going live.
        </p>
      )}

      {error && <p className="text-sm text-destructive">{error}</p>}

      {items.length > 0 && (
        <ul className="flex flex-col gap-3 text-sm">
          {items.map((item) => (
            <li key={item.id} className="flex flex-col gap-1.5 border-b pb-3 last:border-b-0">
              <div className="flex items-start gap-2">
                <span aria-hidden>
                  {item.status === "pass" ? "✅" : item.status === "fail" ? "❌" : "⬜"}
                </span>
                <span>{item.instruction}</span>
                {!item.blocking && (
                  <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
                    non-blocking
                  </span>
                )}
              </div>
              {atGate && (
                <div className="ml-6 flex flex-wrap items-center gap-2">
                  <input
                    type="text"
                    aria-label={`Comment for step ${item.ord}`}
                    placeholder="Comment"
                    defaultValue={item.comment ?? ""}
                    onChange={(e) =>
                      setComments((c) => ({ ...c, [item.id]: e.target.value }))
                    }
                    className="flex-1 rounded border px-2 py-1 text-sm"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    disabled={busy}
                    onClick={() => mark(item, "pass")}
                  >
                    Pass
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={busy}
                    onClick={() => mark(item, "fail")}
                  >
                    Fail
                  </Button>
                </div>
              )}
              {!atGate && item.comment && (
                <span className="ml-6 text-muted-foreground">— {item.comment}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
