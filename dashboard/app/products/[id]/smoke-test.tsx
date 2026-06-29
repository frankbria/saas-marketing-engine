"use client"

import { useRouter } from "next/navigation"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  runSmokeTest,
  type LifecycleState,
  type SmokeTestResult,
} from "@/lib/api"

function parseResult(json: string | null): SmokeTestResult | null {
  if (!json) return null
  try {
    return JSON.parse(json) as SmokeTestResult
  } catch {
    return null
  }
}

// The smoke test runs only once setup is complete; a pass clears the smoke gate, then the launch
// checklist (S2.8) is emitted to cross to `qa`.
export function SmokeTest({
  productId,
  lifecycleState,
  smokeTestJson,
}: {
  productId: number
  lifecycleState: LifecycleState
  smokeTestJson: string | null
}) {
  const router = useRouter()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const result = parseResult(smokeTestJson)
  // The smoke test runs only in setup_done and records its verdict (it no longer transitions state
  // on its own — emitting the launch checklist does). So the action is offered only while runnable;
  // once past the gate the panel is a read-only record of the verdict.
  const runnable = lifecycleState === "setup_done"
  const beforeGate = ["draft", "strategy", "setup_ready"].includes(lifecycleState)

  async function run() {
    setBusy(true)
    setError(null)
    try {
      await runSmokeTest(productId)
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
        <h2 className="text-sm font-semibold">Pre-QA smoke test</h2>
        {runnable && (
          <Button type="button" variant="outline" disabled={busy} onClick={run}>
            {result ? "Re-run smoke test" : "Run smoke test"}
          </Button>
        )}
      </div>

      {beforeGate && (
        <p className="text-sm text-muted-foreground">
          Available once setup is complete (product in <span className="font-mono">setup_done</span>).
        </p>
      )}

      {error && <p className="text-sm text-destructive">{error}</p>}

      {result && (
        <div className="flex flex-col gap-2 text-sm">
          <span
            className={`w-fit rounded px-2 py-0.5 text-xs ${
              result.passed ? "bg-green-100 text-green-800" : "bg-red-100 text-red-800"
            }`}
          >
            {result.passed
              ? "passed → emit the launch checklist to reach QA"
              : "failed → stays in setup_done"}
          </span>
          <ul className="flex flex-col gap-1">
            {result.stages.map((stage) => (
              <li key={stage.stage} className="flex items-start gap-2">
                <span aria-hidden>{stage.ok ? "✅" : "❌"}</span>
                <span className="font-mono">{stage.stage}</span>
                {stage.detail && (
                  <span className="text-muted-foreground">— {stage.detail}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
