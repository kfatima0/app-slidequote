import { defineConfig } from "astro/config";
import react from "@astrojs/react";

// Astro ships static HTML and hydrates only the islands that need to be live.
// Here that is one island: the demo console. Everything around it is plain HTML,
// which is why the page opens instantly on a conference wifi.
export default defineConfig({
  integrations: [react()],
  server: { port: 4321, host: true },
});
