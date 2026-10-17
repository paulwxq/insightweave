/** 后端地址只接受规范形式的 origin，避免意外回退到前端自身。 */
export function validateApiBaseUrl(value: string | undefined): string {
  if (!value) {
    throw new Error('缺少 VITE_API_BASE_URL，请配置浏览器可访问的后端地址。')
  }

  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new Error('VITE_API_BASE_URL 必须是完整的 http/https origin。')
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.origin !== value) {
    throw new Error('VITE_API_BASE_URL 必须是规范形式的 http/https origin，不能带路径或末尾斜杠。')
  }
  return value
}
