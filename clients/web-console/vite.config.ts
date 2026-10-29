import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * 开发时把 API 路径代理到本地 ling 服务。
 * 默认 8080；被占用时用 LING_API_TARGET=http://127.0.0.1:8090 pnpm dev。
 */
const target = process.env.LING_API_TARGET ?? 'http://127.0.0.1:8080';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      // SSE 必须直通，不能压缩或缓冲
      '/v1': { target, changeOrigin: false, ws: false },
      '/healthz': { target, changeOrigin: false },
      '/readyz': { target, changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
});
