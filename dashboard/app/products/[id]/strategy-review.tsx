"use client"

import { useRouter } from "next/navigation"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  approveStrategy,
  updateProduct,
  updateStrategy,
  type Product,
  type StrategyBrief,
} from "@/lib/api"

// ponytail: native styled inputs + raw-JSON textareas — internal firewalled tool, single operator.
const field =
  "w-full rounded-md border bg-transparent px-3 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"

// The brief's editable fields: positioning is free text, the rest are JSON-encoded strings.
const BRIEF_JSON: (keyof StrategyBrief)[] = [
  "icp_json",
  "pain_points_json",
  "channel_plan_json",
  "content_pillars_json",
  "cadence_json",
]

export function StrategyReview({
  product,
  brief,
}: {
  product: Product
  brief: StrategyBrief
}) {
  const router = useRouter()
  const [saving, setSaving] = useState(false)
  const [approving, setApproving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const canApprove = product.lifecycle_state === "strategy"

  async function onSave(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    const data = new FormData(e.currentTarget)
    try {
      await updateStrategy(product.id, {
        positioning: String(data.get("positioning") ?? ""),
        icp_json: String(data.get("icp_json") ?? ""),
        pain_points_json: String(data.get("pain_points_json") ?? ""),
        channel_plan_json: String(data.get("channel_plan_json") ?? ""),
        content_pillars_json: String(data.get("content_pillars_json") ?? ""),
        cadence_json: String(data.get("cadence_json") ?? ""),
      })
      // Omit blank brand/price so saving brief-only edits doesn't trip the backend's
      // JSON validator (json.loads("") → 422) or clear an unset price.
      const brandRaw = String(data.get("brand_json") ?? "").trim()
      const priceRaw = String(data.get("price_amount_cents") ?? "").trim()
      await updateProduct(product.id, {
        ...(brandRaw ? { brand_json: brandRaw } : {}),
        ...(priceRaw ? { price_amount_cents: Number(priceRaw) } : {}),
        price_interval: String(data.get("price_interval") ?? ""),
      })
      router.refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save")
    } finally {
      setSaving(false)
    }
  }

  async function onApprove() {
    setApproving(true)
    setError(null)
    try {
      await approveStrategy(product.id)
      router.refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to approve")
    } finally {
      setApproving(false)
    }
  }

  return (
    <form onSubmit={onSave} className="flex flex-col gap-5">
      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold">Brief</h2>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Positioning
          <textarea
            name="positioning"
            rows={2}
            defaultValue={brief.positioning}
            className={field}
          />
        </label>
        {BRIEF_JSON.map((name) => (
          <label key={name} className="flex flex-col gap-1 text-sm font-medium">
            {name}
            <textarea
              name={name}
              rows={3}
              defaultValue={String(brief[name] ?? "")}
              className={`${field} font-mono`}
            />
          </label>
        ))}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold">Brand</h2>
        <label className="flex flex-col gap-1 text-sm font-medium">
          brand_json
          <textarea
            name="brand_json"
            rows={4}
            defaultValue={product.brand_json ?? ""}
            className={`${field} font-mono`}
          />
        </label>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold">Price</h2>
        <div className="flex gap-3">
          <label className="flex flex-1 flex-col gap-1 text-sm font-medium">
            Amount (cents)
            <input
              name="price_amount_cents"
              type="number"
              min={1}
              defaultValue={product.price_amount_cents ?? ""}
              className={field}
            />
          </label>
          <label className="flex flex-1 flex-col gap-1 text-sm font-medium">
            Interval
            <input
              name="price_interval"
              defaultValue={product.price_interval ?? ""}
              placeholder="month"
              className={field}
            />
          </label>
        </div>
      </section>

      {error && <p className="text-sm text-destructive">{error}</p>}

      <div className="flex items-center gap-2">
        <Button type="submit" disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={onApprove}
          disabled={!canApprove || approving}
        >
          {approving ? "Approving…" : "Approve strategy"}
        </Button>
        {!canApprove && (
          <span className="text-xs text-muted-foreground">
            {brief.approved
              ? "Already approved."
              : `Approve unavailable in ${product.lifecycle_state}.`}
          </span>
        )}
      </div>
    </form>
  )
}
