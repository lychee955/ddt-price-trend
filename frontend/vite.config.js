import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
  build: {
    outDir: '../src/ddt/static', emptyOutDir: true,
    rollupOptions: { output: { manualChunks: { charts: ['echarts/core', 'echarts/charts', 'echarts/components', 'echarts/renderers'], ui: ['element-plus'], vue: ['vue'] } } },
  },
})
