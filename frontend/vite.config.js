import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

const rawBackendTarget = process.env.VITE_API_BASE_URL?.trim() || ''
const backendTarget = /^https?:\/\//i.test(rawBackendTarget)
  ? rawBackendTarget.replace(/\/+$/, '')
  : 'http://127.0.0.1:2221'
const proxyTarget = {
  target: backendTarget,
  changeOrigin: true,
  secure: true,
  headers: {
    'ngrok-skip-browser-warning': 'true',
  },
}

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  esbuild: {
    drop: process.env.NODE_ENV === 'production' ? ['console', 'debugger'] : [],
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    proxy: {
      '/static': proxyTarget,
      '/ai_chat': proxyTarget,
      '/ai_portrait': proxyTarget,
      '/path': proxyTarget,
      '/learning_path': proxyTarget,
      '/resource': proxyTarget,
      '/image': proxyTarget,
      '/knowledge': proxyTarget,
      '/user': proxyTarget,
      '/admin': proxyTarget,
      '/exam': proxyTarget,
      '/video': proxyTarget,
      '/study': proxyTarget,
      '/presentation': proxyTarget,
      '/notification': proxyTarget,
      '/annotation': proxyTarget,
      '/debug': proxyTarget,
    },
  },
})
