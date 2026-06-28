import { afterEach, describe, expect, it, vi } from "vitest"

import { apiFetch } from "./api"

afterEach(() => {
  vi.restoreAllMocks()
})

describe("apiFetch", () => {
  it("prefixes the private API path and parses JSON", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }))

    const data = await apiFetch<{ ok: boolean }>("/products")

    expect(data).toEqual({ ok: true })
    expect(fetchMock.mock.calls[0][0]).toContain("/api/private/products")
  })

  it("throws on a non-ok response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("boom", { status: 500 }))
    await expect(apiFetch("/products")).rejects.toThrow("API 500")
  })

  it("returns undefined for 204 No Content", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }))
    await expect(apiFetch("/products/1")).resolves.toBeUndefined()
  })
})
