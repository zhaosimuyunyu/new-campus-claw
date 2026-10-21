export type TokenPair = {access:string; refresh:string}

const refreshKey = 'campusclaw.refresh'
let accessToken: string|null = null
let refreshing: Promise<string>|null = null

export function saveTokens(tokens: TokenPair) {
  accessToken = tokens.access
  sessionStorage.setItem(refreshKey, tokens.refresh)
}

export function clearTokens() {
  accessToken = null
  sessionStorage.removeItem(refreshKey)
}

export function hasStoredAuth() {
  return Boolean(sessionStorage.getItem(refreshKey))
}

async function refreshAccess(): Promise<string> {
  if (refreshing) return refreshing
  const refresh = sessionStorage.getItem(refreshKey)
  if (!refresh) throw new Error('请先登录')
  refreshing = (async () => {
    const res = await fetch('/api/auth/refresh/', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({refresh})})
    if (!res.ok) {
      if (res.status === 400 || res.status === 401) {
        clearTokens()
        throw new Error('登录已过期，请重新登录')
      }
      throw new Error('服务暂时不可用，请稍后重试')
    }
    const tokens = await res.json() as TokenPair
    saveTokens(tokens)
    return tokens.access
  })().finally(() => { refreshing = null })
  return refreshing
}

export async function restoreAuth() {
  if (sessionStorage.getItem(refreshKey)) await refreshAccess()
}

export async function logoutAuth() {
  try {
    if (sessionStorage.getItem(refreshKey)) {
      const token = await refreshAccess()
      const response = await fetch('/api/auth/logout/', {method:'POST', headers:{'Content-Type':'application/json', Authorization:`Bearer ${token}`},
        body:JSON.stringify({refresh:sessionStorage.getItem(refreshKey)})})
      if (!response.ok) throw new Error('退出登录失败，刷新令牌可能仍有效')
    }
  } finally { clearTokens() }
}

export async function authenticatedFetch(url: string, init: RequestInit = {}, publicRequest = false): Promise<Response> {
  const headers = new Headers(init.headers)
  if (!publicRequest) {
    const token = accessToken || await refreshAccess()
    headers.set('Authorization', `Bearer ${token}`)
  }
  let res = await fetch(url, {...init, headers})
  if (res.status === 401 && !publicRequest) {
    const token = await refreshAccess()
    headers.set('Authorization', `Bearer ${token}`)
    res = await fetch(url, {...init, headers})
  }
  return res
}
