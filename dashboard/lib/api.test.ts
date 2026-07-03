import { afterEach, describe, expect, it, vi } from "vitest"

import {
  apiFetch,
  approveStrategy,
  connectChannel,
  getContentCalendar,
  getFunnel,
  getQaChecklist,
  getSetupChecklist,
  getStrategy,
  goLive,
  listChannels,
  listPublishedContent,
  retractContent,
  runSmokeTest,
  setChannelPaused,
  setChecklistItemStatus,
  setQaItemStatus,
  triggerChannelSetup,
  triggerQaChecklist,
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

describe("channels + setup checklist (S2.6)", () => {
  it("triggerChannelSetup POSTs the setup endpoint", async () => {
    const fetchMock = mockFetch({ job_id: 1, status: "queued" })
    await triggerChannelSetup(7)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/channels/7/setup")
    expect(init?.method).toBe("POST")
  })

  it("listChannels GETs the channels endpoint", async () => {
    const fetchMock = mockFetch([])
    await listChannels(7)
    expect(fetchMock.mock.calls[0][0]).toContain("/api/private/channels/7")
  })

  it("getSetupChecklist GETs the checklist endpoint", async () => {
    const fetchMock = mockFetch([])
    await getSetupChecklist(7)
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/api/private/channels/7/checklist"
    )
  })

  it("connectChannel POSTs the token to the connect endpoint", async () => {
    const fetchMock = mockFetch({ connect_state: "connected" })
    await connectChannel(7, 3, { access_token: "tok" })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/channels/7/3/connect")
    expect(init?.method).toBe("POST")
    expect(init?.body).toBe(JSON.stringify({ access_token: "tok" }))
  })

  it("connectChannel POSTs the structured Reddit credential (S4.8.1)", async () => {
    const fetchMock = mockFetch({ connect_state: "connected" })
    const reddit = {
      client_id: "cid",
      client_secret: "sec",
      refresh_token: "rt",
      user_agent: "ua",
    }
    await connectChannel(7, 3, { reddit })
    const [, init] = fetchMock.mock.calls[0]
    expect(init?.body).toBe(JSON.stringify({ reddit }))
  })

  it("setChecklistItemStatus PATCHes the item", async () => {
    const fetchMock = mockFetch({ status: "done" })
    await setChecklistItemStatus(7, 9, "done")
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/channels/7/checklist/9")
    expect(init?.method).toBe("PATCH")
    expect(init?.body).toBe(JSON.stringify({ status: "done" }))
  })
})

describe("per-channel kill switch (S4.6)", () => {
  it("setChannelPaused PATCHes the pause endpoint with the flag", async () => {
    const fetchMock = mockFetch({ id: 3, paused: true })
    await setChannelPaused(7, 3, true)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/channels/7/3/pause")
    expect(init?.method).toBe("PATCH")
    expect(init?.body).toBe(JSON.stringify({ paused: true }))
  })
})

describe("retract published content (S4.7)", () => {
  it("listPublishedContent GETs the content endpoint", async () => {
    const fetchMock = mockFetch([])
    await listPublishedContent(7)
    expect(fetchMock.mock.calls[0][0]).toContain("/api/private/content/7")
  })

  it("retractContent POSTs the retract endpoint", async () => {
    const fetchMock = mockFetch({ id: 3, status: "retracted" })
    await retractContent(7, 3)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/content/7/3/retract")
    expect(init?.method).toBe("POST")
  })
})

describe("pre-QA smoke test (S2.7)", () => {
  it("runSmokeTest POSTs the smoke-test endpoint", async () => {
    const fetchMock = mockFetch({ passed: true, ran_at: "now", stages: [] })
    await runSmokeTest(7)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/qa/7/smoke-test")
    expect(init?.method).toBe("POST")
  })
})

describe("QA gate (S3.2)", () => {
  it("triggerQaChecklist POSTs the checklist endpoint", async () => {
    const fetchMock = mockFetch({ job_id: 1, status: "queued" })
    await triggerQaChecklist(7)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/qa/7/checklist")
    expect(init?.method).toBe("POST")
  })

  it("getQaChecklist GETs the checklist endpoint", async () => {
    const fetchMock = mockFetch([])
    await getQaChecklist(7)
    expect(fetchMock.mock.calls[0][0]).toContain("/api/private/qa/7/checklist")
  })

  it("setQaItemStatus PATCHes the item with status + comment", async () => {
    const fetchMock = mockFetch({ status: "pass" })
    await setQaItemStatus(7, 4, "pass", "ok")
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/qa/7/checklist/4")
    expect(init?.method).toBe("PATCH")
    expect(init?.body).toBe(JSON.stringify({ status: "pass", comment: "ok" }))
  })

  it("goLive POSTs the go-live endpoint", async () => {
    const fetchMock = mockFetch({ lifecycle_state: "live" })
    await goLive(7)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/qa/7/go-live")
    expect(init?.method).toBe("POST")
  })
})

describe("content calendar (S6.3)", () => {
  it("getContentCalendar GETs the calendar endpoint and returns the parsed array", async () => {
    const body = [
      {
        id: 3,
        channel_id: 1,
        content_type: "post",
        title: "Launch post",
        status: "published",
        spot_check: true,
        critic_score: 88,
        scheduled_for: "2026-07-01T09:00:00Z",
        published_at: "2026-07-01T09:00:05Z",
        created_at: "2026-06-30T12:00:00Z",
        external_url: "https://reddit.com/r/x/3",
        metrics: {
          impressions: 10,
          visits: 4,
          signups: 1,
          paid: 0,
          revenue_cents: 0,
        },
      },
    ]
    const fetchMock = mockFetch(body)
    const data = await getContentCalendar(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/content/1/calendar")
    expect(init?.method).toBe("GET")
    expect(data).toEqual(body)
  })
})

describe("attributed funnel (S6.1)", () => {
  it("getFunnel GETs the funnel endpoint and returns the parsed body", async () => {
    const body = {
      stages: { impressions: 10, visits: 5, signups: 2, paid: 1 },
      revenue_cents: 999,
      rows: [
        {
          channel_id: 1,
          channel_type: "reddit",
          content_item_id: 7,
          title: "Post title",
          external_url: "https://reddit.com/r/x/1",
          impressions: 10,
          visits: 5,
          signups: 2,
          paid: 1,
          revenue_cents: 999,
        },
      ],
    }
    const fetchMock = mockFetch(body)
    const data = await getFunnel(7)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/api/private/metrics/7/funnel")
    expect(init?.method).toBe("GET")
    expect(data).toEqual(body)
  })
})
