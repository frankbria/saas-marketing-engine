import { redirect } from "next/navigation"

// S5.2.1 (#83): `/` was still the generated shadcn scaffold ("Project ready!"). `/products` is
// already the portfolio index, so this redirects rather than growing a second one to keep in
// sync. Kept as a server redirect (not a rewrite) so the address bar shows where you actually
// are and a bookmark of `/` survives if a real landing page ever lands here.
export default function Page() {
  redirect("/products")
}
