// Typed client for the backend private dashboard API (S0.3).
// Base URL comes from NEXT_PUBLIC_API_BASE_URL; defaults to the v1 local API port.

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8010"

export type MonetizationModel = "cc_sub" | "trial" | "freemium"
export type LifecycleState =
  "draft" | "strategy" | "setup_ready" | "setup_done" | "qa" | "live" | "paused"

export interface Product {
  id: number
  name: string
  slug: string
  repo_url: string | null
  repo_local_path: string | null
  description: string | null
  monetization_model: MonetizationModel
  marketing_domain: string | null
  token_budget_cents_month: number
  brand_json: string | null
  price_amount_cents: number | null
  price_interval: string | null
  lifecycle_state: LifecycleState
  created_at: string
  updated_at: string
}

// The strategy brief (S1.1). The *_json fields are JSON-encoded strings the owner reviews/edits.
export interface StrategyBrief {
  id: number
  product_id: number
  icp_json: string
  pain_points_json: string
  positioning: string
  channel_plan_json: string
  content_pillars_json: string
  cadence_json: string
  approved: boolean
  approved_at: string | null
  created_at: string
  updated_at: string
}

export interface ProductUpdate {
  brand_json?: string
  price_amount_cents?: number
  price_interval?: string
}

export interface BriefUpdate {
  positioning?: string
  icp_json?: string
  pain_points_json?: string
  channel_plan_json?: string
  content_pillars_json?: string
  cadence_json?: string
}

export interface ProductCreate {
  name: string
  repo_url?: string
  repo_local_path?: string
  description?: string
  monetization_model?: MonetizationModel
  marketing_domain?: string
  token_budget_cents_month?: number
}

export async function apiFetch<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  const res = await fetch(`${API_BASE}/api/private${path}`, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    ...init,
  })
  if (!res.ok) {
    throw new Error(`API ${res.status}: ${await res.text()}`)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const listProducts = () => apiFetch<Product[]>("/products")

export const getProduct = (id: number) => apiFetch<Product>(`/products/${id}`)

export const createProduct = (payload: ProductCreate) =>
  apiFetch<Product>("/products", {
    method: "POST",
    body: JSON.stringify(payload),
  })

export const updateProduct = (id: number, payload: ProductUpdate) =>
  apiFetch<Product>(`/products/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  })

// S1.4: review/edit/approve the strategy. getStrategy 404s until S1.1 has produced the brief.
export const getStrategy = (productId: number) =>
  apiFetch<StrategyBrief>(`/strategy/${productId}`)

export const updateStrategy = (productId: number, payload: BriefUpdate) =>
  apiFetch<StrategyBrief>(`/strategy/${productId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  })

export const approveStrategy = (productId: number) =>
  apiFetch<Product>(`/strategy/${productId}/approve`, { method: "POST" })
