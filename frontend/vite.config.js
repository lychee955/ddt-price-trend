import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: process.env.VITE_BASE_PATH || '/',
  plugins: [vue()],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
  build: {
    outDir: process.env.VITE_STATIC_MODE === 'true' ? 'dist' : '../src/ddt/static', emptyOutDir: true,
    rollupOptions: { output: { manualChunks: { charts: ['echarts/core', 'echarts/charts', 'echarts/components', 'echarts/renderers'], ui: ['element-plus'], vue: ['vue'] } } },
  },
})
