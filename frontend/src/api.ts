import { authenticatedFetch } from './auth'

export type Course = { id: number; name: string; role: 'teacher'|'student'; join_code: string|null; icon: string|null; member_count: number }
export type FileInfo = { id: number; name: string; size: number; created_at: string; download_url: string }
export type Material = { id: number; title: string; attachment: FileInfo; created_at: string; index_status?: string; index_error?: string }
export type Assignment = { id: number; title: string; description: string; due_at: string; allow_late: boolean; allowed_extensions: string[]; max_file_size_mb: number; attachment: FileInfo|null; my_versions?: Version[] }
export type Version = { number: number; submitted_at: string; is_late: boolean; attachment: FileInfo }
export type Submission = { student: {id:number;name:string;email:string}; versions: Version[] }
export type Member = {id:number;name:string;email:string;role:string}
export type Me = {id:number;email:string;display_name:string;can_create_course:boolean}
export type AgentThread = {id:number; title:string; course_id:number|null; status:string; created_at:string; updated_at:string}
export type AgentCitation = {chunk_id:number; material_id:number; attachment_id?:number; title:string; chunk_index:number; start_offset:number; end_offset:number; excerpt:string; mode:string; file_name?:string; download_url:string; preview_url?:string; score?:number; rrf_score?:number}
export type AgentMessage = {id:number; role:'user'|'assistant'|'tool'; content:string; citations:AgentCitation[]; attachments:{id:number;name:string;mime_type:string}[]; metadata:Record<string,unknown>; created_at:string}
export type AgentArtifact = {id:number; name:string; mime_type:string; download_url:string; preview_url:string; source_url?:string|null; created_at:string}

async function checkResponse(res: Response) {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || body.detail || `请求失败 (${res.status})`)
  }
  return res
}

export async function api<T>(path: string, method = 'GET', data?: Record<string, unknown>|FormData): Promise<T> {
  const headers: Record<string,string> = {}
  if (data && !(data instanceof FormData)) headers['Content-Type'] = 'application/json'
  const publicRequest = /^(auth\/(code|login|register|reset|refresh)\/|csrf\/)/.test(path)
  const res = await checkResponse(await authenticatedFetch('/api/' + path, { method, headers,
    body: data ? data instanceof FormData ? data : JSON.stringify(data) : undefined }, publicRequest))
  return res.json()
}

export async function protectedObjectUrl(url: string) {
  const res = await checkResponse(await authenticatedFetch(url))
  return URL.createObjectURL(await res.blob())
}

export async function protectedText(url: string) {
  return (await checkResponse(await authenticatedFetch(url))).text()
}

export async function openProtected(url: string, existingTab?: Window|null) {
  const tab = existingTab === undefined ? window.open('', '_blank') : existingTab
  try {
    const objectUrl = await protectedObjectUrl(url)
    if (tab) tab.location.href = objectUrl
    else window.open(objectUrl, '_blank')
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60000)
  } catch (error) { tab?.close(); throw error }
}

export async function download(file: FileInfo) {
  const url = await protectedObjectUrl(file.download_url)
  const a = document.createElement('a'); a.href = url; a.download = file.name; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function uploadAgentAttachment(file: File, courseId: number|null) {
  const form = new FormData(); form.append('file', file); if (courseId) form.append('course_id', String(courseId))
  return api<{id:number;name:string;size:number;mime_type:string}>('agent/attachments/', 'POST', form)
}

export async function streamAgent(path: string, data: Record<string, unknown>, handlers: {
  onEvent: (type: string, payload: any) => void
}) {
  const response = await checkResponse(await authenticatedFetch('/api/' + path, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) }))
  if (!response.body) throw new Error('浏览器不支持流式响应')
  const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = ''
  while (true) {
    const { value, done } = await reader.read(); if (done) break
    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split('\n\n'); buffer = frames.pop() || ''
    for (const frame of frames) {
      const event = frame.match(/^event:\s*(.+)$/m)?.[1] || 'message'
      const raw = frame.match(/^data:\s*(.+)$/m)?.[1] || '{}'
      try { handlers.onEvent(event, JSON.parse(raw)) } catch { /* ignore malformed keepalive frames */ }
    }
  }
}
