import Link from "next/link"
import { notFound } from "next/navigation"

import {
  getProduct,
  getSetupChecklist,
  getStrategy,
  listChannels,
  type Channel,
  type Product,
  type SetupChecklistItem,
  type StrategyBrief,
} from "@/lib/api"

import { ChannelSetup } from "./channel-setup"
import { StrategyReview } from "./strategy-review"

export const dynamic = "force-dynamic"

export default async function ProductDetailPage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { id } = await params
  const productId = Number(id)
  if (!Number.isInteger(productId)) notFound()

  let product: Product
  try {
    product = await getProduct(productId)
  } catch {
    notFound()
  }

  // The brief 404s until S1.1 has run; treat that as "no strategy yet" rather than an error.
  let brief: StrategyBrief | null = null
  try {
    brief = await getStrategy(productId)
  } catch {
    brief = null
  }

  // Channels + setup checklist exist once S2.6 setup has run; empty until then.
  let channels: Channel[] = []
  let checklist: SetupChecklistItem[] = []
  try {
    ;[channels, checklist] = await Promise.all([
      listChannels(productId),
      getSetupChecklist(productId),
    ])
  } catch {
    channels = []
    checklist = []
  }

  return (
    <div className="mx-auto flex min-h-svh w-full max-w-3xl flex-col gap-6 p-6">
      <div className="flex items-center justify-between">
        <Link
          href="/products"
          className="text-sm text-muted-foreground hover:underline"
        >
          ← Products
        </Link>
        <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
          {product.lifecycle_state}
        </span>
      </div>
      <h1 className="text-xl font-medium">{product.name}</h1>

      {brief ? (
        <StrategyReview product={product} brief={brief} />
      ) : (
        <p className="text-sm text-muted-foreground">
          No strategy brief yet. Generate it before reviewing.
        </p>
      )}

      <ChannelSetup
        productId={productId}
        channels={channels}
        checklist={checklist}
      />
    </div>
  )
}
