import { defineConfig, loadEnv } from 'vite'
import { execFileSync } from 'node:child_process'
import vue from '@vitejs/plugin-vue'
import { validateApiBaseUrl } from './src/api/apiBaseUrl'

export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  validateApiBaseUrl(env.VITE_API_BASE_URL)

  // Docker 构建上下文不包含 .git，由 CI 传入实际源码 SHA；本地开发直接读取 Git。
  let version = process.env.APP_GIT_SHA?.trim()
  if (!version) {
    try {
      version = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()
    } catch {
      version = '未标注'
    }
  }
  const builtAt = command === 'build'
    ? new Intl.DateTimeFormat('sv-SE', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).format(new Date(process.env.APP_BUILD_TIME || Date.now()))
    : ''

  return {
    plugins: [vue()],
    define: {
      __APP_BUILD_INFO__: JSON.stringify({ version: version.slice(0, 7), builtAt }),
    },
    server: {
      port: 5173,
      strictPort: true,
    },
  }
})
