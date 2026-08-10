"use client"

import { useRouter } from "next/navigation"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { triggerCrank, type Channel, type LifecycleState } from "@/lib/api"

// Mirrors the backend's fan-out filter (`eligible_channels` in modules/crank/crank.py). Kept here
// so the picker offers only channels a crank would actually reach — the API refuses the rest with
// a reason, and an option that always errors is worse than no option.
function isCrankable(channel: Channel): boolean {
  return (
    channel.enabled &&
    channel.autonomous &&
    !channel.paused &&
    channel.connect_state !== "failed"
  )
}

// S4.1.1: run the crank now instead of waiting for the hourly tick and the (weekly by default)
// cadence. Available only once the product is live — the same gate the scheduler applies.
export function ManualCrank({
  productId,
  lifecycleState,
  channels,
}: {
  productId: number
  lifecycleState: LifecycleState
  channels: Channel[]
}) {
  const router = useRouter()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [queued, setQueued] = useState<number | null>(null)
  const [channelId, setChannelId] = useState<string>("")

  const live = lifecycleState === "live"
  const crankable = channels.filter(isCrankable)

  async function run() {
    setBusy(true)
    setError(null)
    setQueued(null)
    try {
      const job = await triggerCrank(
        productId,
        channelId === "" ? undefined : Number(channelId)
      )
      setQueued(job.job_id)
      router.refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed")
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">Run the crank</h2>
        <div className="flex items-center gap-2">
          {live && crankable.length > 1 && (
            <select
              aria-label="Channel to crank"
              className="rounded-md border bg-background px-2 py-1 text-sm"
              value={channelId}
              disabled={busy}
              onChange={(event) => setChannelId(event.target.value)}
            >
              <option value="">All channels</option>
              {crankable.map((channel) => (
                <option key={channel.id} value={channel.id}>
                  {channel.type}
                </option>
              ))}
            </select>
          )}
          <Button
            type="button"
            variant="outline"
            disabled={!live || busy || crankable.length === 0}
            onClick={run}
          >
            {busy ? "Queueing…" : "Crank now"}
          </Button>
        </div>
      </div>

      {!live && (
        <p className="text-sm text-muted-foreground">
          Available once the product is live (currently{" "}
          <span className="font-mono">{lifecycleState}</span>). The crank only
          runs after go-live.
        </p>
      )}

      {live && crankable.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No channel is eligible to crank. A channel must be enabled,
          autonomous, unpaused, and connected.
        </p>
      )}

      {live && crankable.length > 0 && (
        <p className="text-sm text-muted-foreground">
          Generates now, off-cadence — this does not shift the next scheduled
          crank. Generated items still publish on the next publish tick and obey
          the usual pacing.
        </p>
      )}

      {error && <p className="text-sm text-destructive">{error}</p>}

      {queued !== null && (
        <p className="text-sm text-muted-foreground">
          Queued as job <span className="font-mono">#{queued}</span>.
        </p>
      )}
    </section>
  )
}
