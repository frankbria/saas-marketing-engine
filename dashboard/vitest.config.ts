import { defineConfig } from "vitest/config";

// S0.6 (#81): coverage is reported, not gated, on the dashboard. The tested surface is `lib/`
// — the API client, calendar maths, and helpers. `app/` (Next pages) and `components/ui`
// (unmodified shadcn primitives) have no unit tests and would only dilute the number.
export default defineConfig({
  test: {
    coverage: {
      provider: "v8",
      include: ["lib/**/*.ts"],
      exclude: ["lib/**/*.test.ts"],
      reporter: ["text", "lcov"],
    },
  },
});
