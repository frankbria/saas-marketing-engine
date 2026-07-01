"use client"

import { useRouter } from "next/navigation"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  connectChannel,
  setChannelPaused,
  setChecklistItemStatus,
  triggerChannelSetup,
  type Channel,
  type SetupChecklistItem,
} from "@/lib/api"

const field =
  "w-full rounded-md border bg-transparent px-3 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"

const connectBadge: Record<Channel["connect_state"], string> = {
  pending: "bg-muted text-muted-foreground",
  connected: "bg-green-100 text-green-800",
  failed: "bg-red-100 text-red-800",
}

function profileOf(channel: Channel): {
  handle?: string
  bio?: string
  profile_copy?: string
  warmup_note?: string
} {
  if (!channel.profile_json) return {}
  try {
    return JSON.parse(channel.profile_json)
  } catch {
    return {}
  }
}

export function ChannelSetup({
  productId,
  channels,
  checklist,
}: {
  productId: number
  channels: Channel[]
  checklist: SetupChecklistItem[]
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
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">Channels &amp; setup checklist</h2>
        <Button
          type="button"
          variant="outline"
          disabled={busy}
          onClick={() => run(() => triggerChannelSetup(productId))}
        >
          {channels.length ? "Re-run setup" : "Set up channels"}
        </Button>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {channels.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No channels yet. Run setup to generate profiles and the human checklist.
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {channels.map((channel) => {
            const profile = profileOf(channel)
            return (
              <li
                key={channel.id}
                className="flex flex-col gap-2 rounded-md border p-3 text-sm"
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium">
                    {channel.type}
                    {channel.autonomous && (
                      <span className="ml-2 text-xs text-muted-foreground">
                        autonomous
                      </span>
                    )}
                    {channel.paused && (
                      <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">
                        paused
                      </span>
                    )}
                  </span>
                  <div className="flex items-center gap-2">
                    {channel.autonomous && (
                      <Button
                        type="button"
                        variant="outline"
                        size="xs"
                        disabled={busy}
                        onClick={() =>
                          run(() =>
                            setChannelPaused(productId, channel.id, !channel.paused)
                          )
                        }
                      >
                        {channel.paused ? "Resume" : "Pause"}
                      </Button>
                    )}
                    <span
                      className={`rounded px-2 py-0.5 text-xs ${connectBadge[channel.connect_state]}`}
                    >
                      {channel.connect_state}
                    </span>
                  </div>
                </div>
                {profile.handle && (
                  <p className="text-muted-foreground">
                    Handle: <span className="font-mono">{profile.handle}</span>
                    {profile.bio ? ` — ${profile.bio}` : ""}
                  </p>
                )}
                {profile.warmup_note && (
                  <p className="text-xs text-amber-700">⚠ {profile.warmup_note}</p>
                )}
                <form
                  className="flex gap-2"
                  onSubmit={(e) => {
                    e.preventDefault()
                    const token = String(
                      new FormData(e.currentTarget).get("access_token") ?? ""
                    ).trim()
                    if (token)
                      run(() =>
                        connectChannel(productId, channel.id, {
                          access_token: token,
                        })
                      )
                  }}
                >
                  <input
                    name="access_token"
                    placeholder="paste OAuth token"
                    className={field}
                  />
                  <Button type="submit" variant="outline" disabled={busy}>
                    Connect
                  </Button>
                </form>
              </li>
            )
          })}
        </ul>
      )}

      {checklist.length > 0 && (
        <ul className="flex flex-col gap-1">
          {checklist.map((item) => (
            <li key={item.id} className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-1"
                checked={item.status === "done"}
                disabled={busy}
                onChange={(e) =>
                  run(() =>
                    setChecklistItemStatus(
                      productId,
                      item.id,
                      e.target.checked ? "done" : "pending"
                    )
                  )
                }
              />
              <span
                className={item.status === "done" ? "text-muted-foreground line-through" : ""}
              >
                {item.instruction}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
