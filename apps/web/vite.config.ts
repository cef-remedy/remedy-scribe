import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// Phase 2.1 (decision 0024): browser client on a clinic laptop.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      // The offline shell is not a nicety here: P0-2 requires the app to
      // work with no network, and a doctor must be able to open it and
      // start recording during a clinic wifi outage. Precaching the built
      // assets is what makes that true.
      workbox: {
        globPatterns: ["**/*.{js,css,html,ico,png,svg,webmanifest}"],
        // Never cache API responses: every one of them is PHI or
        // auth-bearing, and a service-worker cache is readable by anyone
        // with the device. The offline story for data is IndexedDB with an
        // explicit retention clock (Phase 2.4), not opportunistic caching.
        navigateFallbackDenylist: [/^\/api\//],
        runtimeCaching: [],
      },
      manifest: {
        name: "Remedy Scribe",
        short_name: "Scribe",
        description: "Clinical note-taking for Remedy clinics",
        theme_color: "#0e5c52",
        background_color: "#f5f7f3",
        display: "standalone",
        start_url: "/",
        icons: [
          { src: "icon.svg", sizes: "any", type: "image/svg+xml", purpose: "any maskable" },
        ],
      },
    }),
  ],
  server: {
    port: 5173,
    // Matches app.core.config.cors_allow_origins on the API side. If these
    // two ever disagree the browser blocks every request before it reaches
    // a route, with nothing in the API log to show for it.
    strictPort: true,
    // Free-tier deploy runbook §8: recording a demo before Netlify exists
    // means pointing this dev server at the real Render API. The tempting
    // way to do that is adding http://localhost:5173 to CORS_ALLOW_ORIGINS
    // on Render — and the production boot guard refuses to boot with a
    // localhost origin in that list, correctly, because it cannot tell a
    // deliberate temporary addition from someone deploying a dev .env by
    // mistake. So this proxies instead: set RENDER_DEV_PROXY_TARGET (a
    // plain shell var, deliberately not VITE_-prefixed so it can never
    // leak into the bundle) and requests to /api/* leave this Node
    // process for Render server-side — the browser only ever talks to
    // localhost:5173, exactly the same same-origin trick the real
    // Netlify rewrite (netlify.toml) performs in production, just
    // running locally instead. Needs `VITE_API_BASE_URL=/` in
    // apps/web/.env too, or the client's own dev default
    // (http://localhost:8000) wins over this proxy entirely.
    proxy: process.env.RENDER_DEV_PROXY_TARGET
      ? {
          "/api": {
            target: process.env.RENDER_DEV_PROXY_TARGET,
            changeOrigin: true,
            secure: true,
          },
        }
      : undefined,
  },
});
