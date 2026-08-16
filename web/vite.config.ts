import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";

const rootDirectory = path.dirname(fileURLToPath(import.meta.url));
const webPort = process.env.MOSHI_WEB_PORT || process.env.web_port;
const backend = webPort ? `http://127.0.0.1:${webPort}` : undefined;

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: path.resolve(rootDirectory, "../moshi_data_pipeline/studio/static"),
    emptyOutDir: true,
  },
  server: backend ? {
    proxy: {
      "/api": backend,
      "/media": backend,
    },
  } : undefined,
});
