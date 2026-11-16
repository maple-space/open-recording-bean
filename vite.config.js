import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // Earlier builds are left in dist/ and not deleted; the service only serves what index.html names.
  build: { emptyOutDir: false },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8875',
    },
  },
})
