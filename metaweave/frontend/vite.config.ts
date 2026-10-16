import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { validateApiBaseUrl } from './src/api/apiBaseUrl'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  validateApiBaseUrl(env.VITE_API_BASE_URL)

  return {
    plugins: [vue()],
    server: {
      port: 5173,
      strictPort: true,
    },
  }
})
