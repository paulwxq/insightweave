import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { loadConfigFromFile } from 'vite'

const frontendRoot = fileURLToPath(new URL('../../', import.meta.url))

async function metadata(env: Record<string, string | undefined>, command: 'build' | 'serve' = 'build') {
  const previous = Object.fromEntries(Object.keys(env).map((key) => [key, process.env[key]]))
  try {
    for (const [key, value] of Object.entries(env)) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
    const loaded = await loadConfigFromFile(
      { command, mode: 'production' },
      `${frontendRoot}/vite.config.ts`,
      frontendRoot,
    )
    assert.ok(loaded)
    assert.ok(loaded.config.define?.__APP_BUILD_INFO__, '构建产物必须包含版本信息')
    return JSON.parse(loaded.config.define.__APP_BUILD_INFO__) as { version: string; builtAt: string }
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  }
}

test('构建使用指定提交，时间按北京时间显示而不是宿主时区', async () => {
  const info = await metadata({
    VITE_API_BASE_URL: 'http://localhost:18080',
    APP_GIT_SHA: '123abcd4567890',
    APP_BUILD_TIME: '2026-11-17T18:30:00Z',
    TZ: 'America/New_York',
  })
  assert.deepEqual(info, { version: '123abcd', builtAt: '2026-11-18 02:30' })
})

test('本地开发显示实际提交并明确标注开发环境', async () => {
  const info = await metadata({
    VITE_API_BASE_URL: 'http://localhost:18080',
    APP_GIT_SHA: undefined,
    APP_BUILD_TIME: undefined,
  }, 'serve')
  const sha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: frontendRoot, encoding: 'utf8' }).trim()
  assert.deepEqual(info, { version: sha.slice(0, 7), builtAt: '' })
})
