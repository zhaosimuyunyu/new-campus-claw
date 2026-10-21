// @vitest-environment jsdom
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'

beforeEach(()=>{vi.resetModules();sessionStorage.clear()})
afterEach(()=>vi.unstubAllGlobals())

describe('authentication during backend recovery',()=>{
  it('keeps the refresh token on a temporary 502 and can recover',async()=>{
    const auth=await import('../src/auth')
    auth.saveTokens({access:'old-access',refresh:'valid-refresh'})
    const fetchMock=vi.fn()
      .mockResolvedValueOnce(new Response('',{status:502}))
      .mockResolvedValueOnce(new Response(JSON.stringify({access:'new-access',refresh:'rotated-refresh'}),{status:200}))
    vi.stubGlobal('fetch',fetchMock)
    await expect(auth.restoreAuth()).rejects.toThrow('服务暂时不可用')
    expect(auth.hasStoredAuth()).toBe(true)
    await auth.restoreAuth()
    expect(sessionStorage.getItem('campusclaw.refresh')).toBe('rotated-refresh')
  })
  it('clears an expired refresh token when authentication is rejected',async()=>{
    const auth=await import('../src/auth')
    auth.saveTokens({access:'old-access',refresh:'expired-refresh'})
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response('',{status:401})))
    await expect(auth.restoreAuth()).rejects.toThrow('登录已过期')
    expect(auth.hasStoredAuth()).toBe(false)
  })
  it('keeps authentication when the connection itself fails',async()=>{
    const auth=await import('../src/auth')
    auth.saveTokens({access:'old-access',refresh:'valid-refresh'})
    vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new TypeError('network unavailable')))
    await expect(auth.restoreAuth()).rejects.toThrow('network unavailable')
    expect(auth.hasStoredAuth()).toBe(true)
  })
})
