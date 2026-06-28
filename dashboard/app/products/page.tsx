import Link from "next/link"

import { Button } from "@/components/ui/button"
import { listProducts, type Product } from "@/lib/api"

export const dynamic = "force-dynamic"

export default async function ProductsPage() {
  let products: Product[] = []
  let error: string | null = null
  try {
    products = await listProducts()
  } catch (e) {
    error = e instanceof Error ? e.message : "Failed to load products"
  }

  return (
    <div className="mx-auto flex min-h-svh w-full max-w-3xl flex-col gap-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-medium">Products</h1>
        <Button asChild>
          <Link href="/products/new">New product</Link>
        </Button>
      </div>

      {error && (
        <p className="text-sm text-destructive">
          Couldn&apos;t reach the API ({error}). Is the backend running?
        </p>
      )}

      {!error && products.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No products yet. Register your first one to get started.
        </p>
      )}

      <ul className="flex flex-col gap-2">
        {products.map((p) => (
          <li
            key={p.id}
            className="flex items-center justify-between rounded-md border p-3 text-sm"
          >
            <div className="flex flex-col">
              <span className="font-medium">{p.name}</span>
              <span className="font-mono text-xs text-muted-foreground">
                {p.slug}
                {p.marketing_domain ? ` · ${p.marketing_domain}` : ""}
              </span>
            </div>
            <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
              {p.lifecycle_state}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
