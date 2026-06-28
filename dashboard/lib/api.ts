// Typed client for the backend private dashboard API (S0.3).
// Base URL comes from NEXT_PUBLIC_API_BASE_URL; defaults to the v1 local API port.

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8010"

export type MonetizationModel = "cc_sub" | "trial" | "freemium"
export type LifecycleState =
  | "draft"
  | "strategy"
  | "setup_ready"
  | "setup_done"
  | "qa"
  | "live"
  | "paused"

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
  lifecycle_state: LifecycleState
  created_at: string
  updated_at: string
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

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
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

export const createProduct = (payload: ProductCreate) =>
  apiFetch<Product>("/products", { method: "POST", body: JSON.stringify(payload) })
