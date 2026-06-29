import { afterEach, describe, expect, it, vi } from "vitest"

import {
  apiFetch,
  approveStrategy,
  getStrategy,
  updateProduct,
  updateStrategy,
} from "./api"

function mockFetch(body: unknown, status = 200) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(new Response(JSON.stringify(body), { status }))
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe("apiFetch", () => {
  it("prefixes the private API path and parses JSON", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ ok: true }), { status: 200 })
      )

    const data = await apiFetch<{ ok: boolean }>("/products")

    expect(data).toEqual({ ok: true })
    expect(fetchMock.mock.calls[0][0]).toContain("/api/private/products")
  })

  it("throws on a non-ok response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("boom", { status: 500 })
    )
    await expect(apiFetch("/products")).rejects.toThrow("API 500")
  })

  it("returns undefined for 204 No Content", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(null, { status: 204 })
    )
    await expect(apiFetch("/products/1")).resolves.toBeUndefined()
  })
})

describe("strategy review (S1.4)", () => {
  it("getStrategy hits the brief endpoint", async () => {
    const fetchMock = mockFetch({ positioning: "x" })
    await getStrategy(7)
    expect(fetchMock.mock.calls[0][0]).toContain("/api/private/strategy/7")
  })

  it("updateStrategy PATCHes the brief", async () => {
    const fetchMock = mockFetch({ positioning: "edited" })
    await updateStrategy(7, { positioning: "edited" })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/strategy/7")
    expect(init?.method).toBe("PATCH")
    expect(init?.body).toBe(JSON.stringify({ positioning: "edited" }))
  })

  it("updateProduct PATCHes the product", async () => {
    const fetchMock = mockFetch({ id: 7 })
    await updateProduct(7, { brand_json: "{}" })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/products/7")
    expect(init?.method).toBe("PATCH")
  })

  it("approveStrategy POSTs to the approve endpoint", async () => {
    const fetchMock = mockFetch({ lifecycle_state: "setup_ready" })
    await approveStrategy(7)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/strategy/7/approve")
    expect(init?.method).toBe("POST")
  })
})
