// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import cases from '../../backend/core/html_cases.json'
import { extractHtml, messagePresentation, renderMarkdown } from '../src/htmlArtifacts'
import type { AgentMessage } from '../src/api'
import AgentResponse from '../src/AgentResponse.vue'
import HtmlPreview from '../src/HtmlPreview.vue'
import { protectedText, download } from '../src/api'

vi.mock('../src/api', () => ({protectedText:vi.fn(), download:vi.fn(), openProtected:vi.fn()}))
const message = (content:string, metadata = {}):AgentMessage => ({id:7, role:'assistant', content, metadata, citations:[], attachments:[], created_at:''})
const artifact = {id:10, name:'page.html', mime_type:'text/html', source_url:'/source/10', preview_url:'/preview/10', download_url:'/download/10', created_at:''}
afterEach(() => vi.clearAllMocks())

describe('HTML extraction and message rendering', () => {
  for (const fixture of cases) it(fixture.name, () => {
    expect(extractHtml(fixture.input).sources).toEqual(fixture.sources)
  })
  it('renders raw and incomplete HTML as text in chat', () => {
    const host = document.createElement('div')
    for (const input of ['<html><style>body{display:none}</style><script>alert(1)</script></html>', '<iframe src=x>', '<img src=x onerror=alert(1)>']) {
      host.innerHTML = renderMarkdown(input)
      expect(host.querySelector('script,style,iframe,img')).toBeNull()
      expect(host.textContent).toContain('<')
    }
  })
  it('provides legacy cards but leaves partial replies as text', () => {
    expect(messagePresentation(message('<html>old</html>')).files).toHaveLength(1)
    expect(messagePresentation(message('<html>old</html>', {partial:true})).files).toHaveLength(0)
  })
  it('does not mount an iframe until a card is opened and download does not open it', async () => {
    const wrapper = mount(AgentResponse, {props:{message:message('<html>test</html>', {display_content:'说明', artifacts:[artifact]})}})
    expect(wrapper.find('iframe').exists()).toBe(false)
    await wrapper.get('[aria-label="下载 page.html"]').trigger('click')
    expect(download).toHaveBeenCalledOnce()
    expect(wrapper.emitted('open')).toBeUndefined()
    await wrapper.get('.html-file-open').trigger('click')
    expect(wrapper.emitted('open')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('<html>')
    wrapper.unmount()
  })
})

describe('preview panel lifecycle', () => {
  it('supports preview, highlighted source, copy, expansion and close', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {value:{writeText}, configurable:true})
    const source = '<html>\n<body>页面</body>\n</html>'
    const wrapper = mount(HtmlPreview, {props:{file:{key:'local', name:'page.html', source}}})
    await flushPromises()
    expect(wrapper.get('iframe').attributes('sandbox')).toBe('allow-scripts')
    expect(wrapper.get('iframe').attributes('srcdoc')).toContain('connect-src')
    await wrapper.get('[aria-label="源码"]').trigger('click')
    expect(wrapper.get('.source-code').text()).toBe(source)
    expect(wrapper.findAll('.source-numbers span')).toHaveLength(3)
    expect(wrapper.find('.hljs-tag').exists()).toBe(true)
    await wrapper.get('[aria-label="复制源码"]').trigger('click')
    expect(writeText).toHaveBeenCalledWith(source)
    await wrapper.get('[aria-label="展开预览"]').trigger('click')
    expect(wrapper.emitted('expand')).toEqual([[true]])
    await wrapper.get('[aria-label="关闭预览"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })
  it('ignores a stale file response', async () => {
    let resolveOld!:(value:string) => void
    vi.mocked(protectedText).mockImplementationOnce(() => new Promise(resolve => {resolveOld = resolve}))
    const wrapper = mount(HtmlPreview, {props:{file:{key:'remote', name:'old.html', artifact}}})
    await wrapper.setProps({file:{key:'new', name:'new.html', source:'<html>new</html>'}})
    await flushPromises()
    resolveOld('<html>old</html>')
    await flushPromises()
    expect(wrapper.get('iframe').attributes('srcdoc')).toContain('<html>new</html>')
    wrapper.unmount()
  })
  it('shows a load failure and retries', async () => {
    vi.mocked(protectedText).mockRejectedValueOnce(new Error('加载失败')).mockResolvedValueOnce('<html>retry</html>')
    const wrapper = mount(HtmlPreview, {props:{file:{key:'remote', name:'page.html', artifact}}})
    await flushPromises()
    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(wrapper.get('[role="alert"]').text()).toContain('加载失败')
    await wrapper.get('.preview-placeholder button').trigger('click')
    await flushPromises()
    expect(wrapper.get('iframe').attributes('srcdoc')).toContain('<html>retry</html>')
    wrapper.unmount()
  })
})
