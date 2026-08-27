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
  },
});
