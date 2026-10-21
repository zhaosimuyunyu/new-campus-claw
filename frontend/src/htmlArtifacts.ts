import { Marked } from 'marked'
import DOMPurify from 'dompurify'
import type { AgentArtifact, AgentMessage } from './api'

export type HtmlFile = { key:string; name:string; source?:string; artifact?:AgentArtifact }
const escapeHtml = (value:string) => value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
// Raw model HTML must never become part of the application's DOM, even mid-stream.
const markdown = new Marked({ breaks:true, gfm:true, renderer:{ html:({text}) => escapeHtml(text) } })
export const renderMarkdown = (source:string) => DOMPurify.sanitize(markdown.parse(source) as string)

export function extractHtml(source:string) {
  const spans:{start:number; end:number; source:string}[] = []
  function documents(start:number, end:number) {
    const segment = source.slice(start, end)
    const opening = /(?:<!doctype\s+html\b[^>]*>\s*)?<html\b[^>]*>/gi
    let match:RegExpExecArray|null
    while ((match = opening.exec(segment))) {
      const tokens = /<!--[\s\S]*?-->|<script\b[^>]*>[\s\S]*?<\/script\s*>|<style\b[^>]*>[\s\S]*?<\/style\s*>|<\/html\s*>/gi
      tokens.lastIndex = opening.lastIndex
      let closing:RegExpExecArray|null
      while ((closing = tokens.exec(segment)) && !closing[0].toLowerCase().startsWith('</html')) { /* skip raw text */ }
      if (!closing) break
      spans.push({start:start + match.index, end:start + tokens.lastIndex, source:segment.slice(match.index, tokens.lastIndex)})
      opening.lastIndex = tokens.lastIndex
    }
  }
  const fence = /^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n/gm
  let cursor = 0, opening:RegExpExecArray|null
  while ((opening = fence.exec(source))) {
    documents(cursor, opening.index)
    const marker = opening[1]
    const closing = new RegExp('^ {0,3}' + marker[0] + '{' + marker.length + ',}[ \\t]*\\r?$', 'gm')
    closing.lastIndex = fence.lastIndex
    const end = closing.exec(source)
    if (!end) { cursor = source.length; break }
    if (opening[2].trim().toLowerCase() === 'html') {
      const body = source.slice(fence.lastIndex, end.index).replace(/\r?\n$/, '')
      if (body.trim()) spans.push({start:opening.index, end:closing.lastIndex, source:body})
    }
    cursor = closing.lastIndex
    fence.lastIndex = cursor
  }
  documents(cursor, source.length)
  const pieces:string[] = []
  cursor = 0
  for (const span of spans) { pieces.push(source.slice(cursor, span.start), '\n\n'); cursor = span.end }
  pieces.push(source.slice(cursor))
  return { display:pieces.join('').trim(), sources:spans.map(span => span.source) }
}

function filename(source:string, id:number, index:number) {
  const title = source.match(/<title\b[^>]*>([\s\S]*?)<\/title\s*>/i)?.[1] || ''
  const decoder = document.createElement('textarea')
  decoder.innerHTML = title.replace(/<[^>]*>/g, '')
  let name = decoder.value.replace(/[<>:"/\\|?*\x00-\x1f]/g, '-').replace(/^[ .]+|[ .]+$/g, '').slice(0, 100)
  if (!name || /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$/i.test(name)) name = `campusclaw-${id}-${index}`
  return name + '.html'
}

export function messagePresentation(message:AgentMessage) {
  const artifacts = (Array.isArray(message.metadata.artifacts) ? message.metadata.artifacts : []) as AgentArtifact[]
  const files:HtmlFile[] = artifacts.filter(file => file.mime_type === 'text/html').map(artifact => ({key:`artifact-${artifact.id}`, name:artifact.name, artifact}))
  if (message.metadata.partial) return {display:message.content, files:[], others:artifacts.filter(file => file.mime_type !== 'text/html')}
  if (typeof message.metadata.display_content === 'string') return {display:message.metadata.display_content, files, others:artifacts.filter(file => file.mime_type !== 'text/html')}
  const extracted = extractHtml(message.content)
  if (!files.length) extracted.sources.forEach((source, index) => files.push({key:`legacy-${message.id}-${index}`, name:filename(source, message.id, index + 1), source}))
  return {display:extracted.display, files, others:artifacts.filter(file => file.mime_type !== 'text/html')}
}

export function downloadHtml(file:HtmlFile, source:string) {
  const url = URL.createObjectURL(new Blob([source], {type:'text/html;charset=utf-8'}))
  const link = document.createElement('a')
  link.href = url; link.download = file.name; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function previewDocument(source:string) {
  // Prepend the policy so model-provided head content cannot run before it.
  return '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" content="connect-src \'none\'; frame-src \'none\'; object-src \'none\'; base-uri \'none\'; form-action \'none\'">' + source
}
