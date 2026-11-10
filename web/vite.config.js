import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物直接输出到 Python 包内的静态目录，由 FastAPI 托管（自部署无需额外 web 服务器）
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: '../wequota/webapp',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true }
    }
  }
})
