import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// S6.2.1 (#79): `reach` is null when the channel has no platform counter (the owned blog and
// podcast — nobody else decides whether those pages are shown). "—" says *unmeasured*; rendering 0
// would assert nobody saw a post we never measured, which is the exact conflation between "we
// published it" and "someone saw it" that issue #79 exists to remove.
export function formatReach(reach: number | null): string {
  return reach === null ? "—" : reach.toLocaleString()
}
