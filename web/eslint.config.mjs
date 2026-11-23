import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    // These two pages set state from the URL in an effect when they mount. Reported, not failing, until they read
    // it with useSearchParams instead.
    files: ["src/app/search/page.tsx", "src/app/settings/page.tsx"],
    rules: { "react-hooks/set-state-in-effect": "warn" },
  },
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts"]),
]);
