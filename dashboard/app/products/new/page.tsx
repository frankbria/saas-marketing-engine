"use client"

import { useRouter } from "next/navigation"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { createProduct, type MonetizationModel } from "@/lib/api"

// ponytail: native styled inputs over five new shadcn primitives — internal firewalled tool.
const field =
  "w-full rounded-md border bg-transparent px-3 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"

export default function NewProductPage() {
  const router = useRouter()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    const data = new FormData(e.currentTarget)
    const budget = data.get("token_budget_cents_month")
    try {
      await createProduct({
        name: String(data.get("name") ?? "").trim(),
        repo_url: String(data.get("repo_url") ?? "").trim() || undefined,
        description: String(data.get("description") ?? "").trim() || undefined,
        monetization_model: data.get("monetization_model") as MonetizationModel,
        marketing_domain: String(data.get("marketing_domain") ?? "").trim() || undefined,
        token_budget_cents_month: budget ? Number(budget) : 0,
      })
      router.push("/products")
      router.refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create product")
      setSubmitting(false)
    }
  }

  return (
    <div className="mx-auto flex min-h-svh w-full max-w-xl flex-col gap-6 p-6">
      <h1 className="text-xl font-medium">Register a product</h1>

      <form onSubmit={onSubmit} className="flex flex-col gap-4">
        <label className="flex flex-col gap-1 text-sm font-medium">
          Name
          <input name="name" required className={field} placeholder="Auto Author" />
        </label>

        <label className="flex flex-col gap-1 text-sm font-medium">
          Repo location
          <input
            name="repo_url"
            className={field}
            placeholder="https://github.com/you/your-saas"
          />
        </label>

        <label className="flex flex-col gap-1 text-sm font-medium">
          Description
          <textarea name="description" rows={3} className={field} />
        </label>

        <label className="flex flex-col gap-1 text-sm font-medium">
          Monetization model
          <select name="monetization_model" defaultValue="cc_sub" className={field}>
            <option value="cc_sub">Credit-card subscription</option>
            <option value="trial">Trial</option>
            <option value="freemium">Freemium</option>
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm font-medium">
          Marketing domain
          <input name="marketing_domain" className={field} placeholder="yoursaas.app" />
        </label>

        <label className="flex flex-col gap-1 text-sm font-medium">
          Token budget (cents / month)
          <input
            name="token_budget_cents_month"
            type="number"
            min={0}
            defaultValue={0}
            className={field}
          />
        </label>

        {error && <p className="text-sm text-destructive">{error}</p>}

        <div className="flex gap-2">
          <Button type="submit" disabled={submitting}>
            {submitting ? "Creating…" : "Create product"}
          </Button>
          <Button type="button" variant="outline" onClick={() => router.push("/products")}>
            Cancel
          </Button>
        </div>
      </form>
    </div>
  )
}
