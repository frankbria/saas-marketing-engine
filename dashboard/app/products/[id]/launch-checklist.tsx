"use client"

import { useRouter } from "next/navigation"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  emitLaunchChecklist,
  type LaunchChecklist as LaunchChecklistResult,
  type LifecycleState,
} from "@/lib/api"

function parseResult(json: string | null): LaunchChecklistResult | null {
  if (!json) return null
  try {
    const parsed = JSON.parse(json) as LaunchChecklistResult
    // Guard against drifted/malformed payloads — without this a bad shape throws in items.map().
    if (!Array.isArray(parsed?.items)) return null
    return parsed
  } catch {
    return null
  }
}

// S2.8: the launch checklist is emitted from real setup state and crosses the gate setup_done → qa.
// It's the second half of the gate (smoke pass + checklist emitted); offered only once the smoke test
// has passed, then it becomes a read-only record of what the human QA gate should verify.
export function LaunchChecklist({
  productId,
  lifecycleState,
  launchChecklistJson,
  smokePassed,
}: {
  productId: number
  lifecycleState: LifecycleState
  launchChecklistJson: string | null
  smokePassed: boolean
}) {
  const router = useRouter()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const result = parseResult(launchChecklistJson)
  // Emitting it crosses to qa, so it's only actionable in setup_done with a passing smoke test.
  const emittable = lifecycleState === "setup_done" && smokePassed

  async function emit() {
    setBusy(true)
    setError(null)
    try {
      await emitLaunchChecklist(productId)
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
        <h2 className="text-sm font-semibold">Launch checklist</h2>
        {emittable && (
          <Button type="button" variant="outline" disabled={busy} onClick={emit}>
            Emit launch checklist
          </Button>
        )}
      </div>

      {lifecycleState === "setup_done" && !smokePassed && (
        <p className="text-sm text-muted-foreground">
          Pass the pre-QA smoke test first; emitting the checklist advances the product to{" "}
          <span className="font-mono">qa</span>.
        </p>
      )}

      {error && <p className="text-sm text-destructive">{error}</p>}

      {result && (
        <ul className="flex flex-col gap-1 text-sm">
          {result.items.map((item) => (
            <li key={item.ord} className="flex items-start gap-2">
              <span aria-hidden>{item.ready ? "✅" : "⬜"}</span>
              <span>{item.label}</span>
              {item.detail && (
                <span className="text-muted-foreground">— {item.detail}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
