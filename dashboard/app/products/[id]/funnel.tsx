import { getFunnel, type Funnel } from "@/lib/api"

// S6.1: attributed funnel + revenue. Fetches its own data (unlike sibling sections, which receive
// already-fetched props) since it 404s independently of the rest of the page until the S6.1
// pipeline (webhook attribution + rollup endpoint) has produced data for this product.
export async function Funnel({ productId }: { productId: number }) {
  let funnel: Funnel | null = null
  try {
    funnel = await getFunnel(productId)
  } catch {
    funnel = null
  }

  const stages = funnel?.stages ?? {
    impressions: 0,
    visits: 0,
    signups: 0,
    paid: 0,
  }
  const revenueCents = funnel?.revenue_cents ?? 0
  const rows = funnel?.rows ?? []

  const isEmpty =
    stages.impressions === 0 &&
    stages.visits === 0 &&
    stages.signups === 0 &&
    stages.paid === 0 &&
    revenueCents === 0

  const maxStageCount = Math.max(
    stages.impressions,
    stages.visits,
    stages.signups,
    stages.paid,
    1
  )

  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-sm font-semibold">Funnel</h2>

      {isEmpty ? (
        <p className="text-sm text-muted-foreground">No funnel activity yet</p>
      ) : (
        <>
          <div className="grid grid-cols-5 gap-2">
            <StageTile
              label="Impressions"
              value={stages.impressions.toLocaleString()}
              barPct={(stages.impressions / maxStageCount) * 100}
            />
            <StageTile
              label="Visits"
              value={stages.visits.toLocaleString()}
              barPct={(stages.visits / maxStageCount) * 100}
            />
            <StageTile
              label="Signups"
              value={stages.signups.toLocaleString()}
              barPct={(stages.signups / maxStageCount) * 100}
            />
            <StageTile
              label="Paid"
              value={stages.paid.toLocaleString()}
              barPct={(stages.paid / maxStageCount) * 100}
            />
            <StageTile label="Revenue" value={formatCents(revenueCents)} />
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="py-1 pr-2 font-medium">Channel</th>
                <th className="py-1 pr-2 font-medium">Content</th>
                <th className="py-1 pr-2 font-medium">Impressions</th>
                <th className="py-1 pr-2 font-medium">Visits</th>
                <th className="py-1 pr-2 font-medium">Signups</th>
                <th className="py-1 pr-2 font-medium">Paid</th>
                <th className="py-1 pr-2 font-medium">Revenue</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr
                  key={`${row.channel_id ?? "none"}:${row.content_item_id ?? "none"}`}
                  className={i === rows.length - 1 ? "" : "border-b"}
                >
                  <td className="py-2 pr-2">
                    {row.channel_type ?? "unattributed"}
                  </td>
                  <td className="py-2 pr-2 text-muted-foreground">
                    {row.title ?? row.external_url ?? "—"}
                  </td>
                  <td className="py-2 pr-2">
                    {row.impressions.toLocaleString()}
                  </td>
                  <td className="py-2 pr-2">{row.visits.toLocaleString()}</td>
                  <td className="py-2 pr-2">{row.signups.toLocaleString()}</td>
                  <td className="py-2 pr-2">{row.paid.toLocaleString()}</td>
                  <td className="py-2 pr-2">
                    {formatCents(row.revenue_cents)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}

function StageTile({
  label,
  value,
  barPct,
}: {
  label: string
  value: string
  barPct?: number
}) {
  return (
    <div className="rounded-md border p-3 text-sm">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="font-medium">{value}</div>
      {barPct !== undefined && (
        <div className="mt-2 h-1.5 w-full rounded-full bg-muted">
          <div
            className="h-1.5 rounded-full bg-primary"
            style={{ width: `${barPct}%` }}
          />
        </div>
      )}
    </div>
  )
}

function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`
}
