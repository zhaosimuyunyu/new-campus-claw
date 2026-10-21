<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Code2, Copy, Download, Eye, Maximize2, Minimize2, X } from 'lucide-vue-next'
import hljs from 'highlight.js/lib/core'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import javascript from 'highlight.js/lib/languages/javascript'
import 'highlight.js/styles/github.css'
import { protectedText } from './api'
import { downloadHtml, previewDocument, type HtmlFile } from './htmlArtifacts'

hljs.registerLanguage('xml', xml)
hljs.registerLanguage('css', css)
hljs.registerLanguage('javascript', javascript)
const props = defineProps<{file:HtmlFile}>()
const emit = defineEmits<{close:[]; expand:[expanded:boolean]}>()
const source = ref(''), loading = ref(false), error = ref(''), notice = ref('')
const mode = ref<'preview'|'source'>('preview'), expanded = ref(false)
let request = 0
const documentSource = computed(() => previewDocument(source.value))
const highlighted = computed(() => hljs.highlight(source.value, {language:'xml'}).value)
const lineCount = computed(() => source.value.split('\n').length)
async function load() {
  const current = ++request
  const file = props.file
  loading.value = true; error.value = ''; source.value = ''; notice.value = ''
  try {
    const text = file.source !== undefined ? file.source : await protectedText(file.artifact?.source_url || `/api/agent/artifacts/${file.artifact!.id}/source/`)
    if (current === request) source.value = text
  } catch (e) { if (current === request) error.value = e instanceof Error ? e.message : '加载失败，请重试' }
  finally { if (current === request) loading.value = false }
}
watch(() => props.file, () => { mode.value = 'preview'; void load() }, {immediate:true})
onBeforeUnmount(() => { request++ })
async function copy() {
  try { await navigator.clipboard.writeText(source.value); notice.value = '已复制源码' }
  catch { notice.value = '复制失败，请在源码中手动选择复制' }
}
function toggleExpanded() { expanded.value = !expanded.value; emit('expand', expanded.value) }
</script>

<template>
  <aside class="html-preview-panel" :class="{'is-expanded':expanded}" aria-label="HTML 文件预览" @keydown.esc="emit('close')">
    <header class="html-preview-header">
      <div class="preview-tabs" role="tablist" aria-label="查看方式">
        <button role="tab" :aria-selected="mode==='preview'" aria-label="效果预览" :class="{active:mode==='preview'}" @click="mode='preview'"><Eye :size="18"/></button>
        <button role="tab" :aria-selected="mode==='source'" aria-label="源码" :class="{active:mode==='source'}" @click="mode='source'"><Code2 :size="18"/></button>
      </div>
      <strong :title="file.name">{{ file.name }}</strong><span class="preview-type">HTML</span>
      <div class="preview-toolbar">
        <button title="复制源码" aria-label="复制源码" :disabled="loading || !!error" @click="copy"><Copy :size="17"/></button>
        <button title="下载 HTML" aria-label="下载 HTML" :disabled="loading || !!error" @click="downloadHtml(file, source)"><Download :size="17"/></button>
        <button :title="expanded?'收起':'展开'" :aria-label="expanded?'收起预览':'展开预览'" @click="toggleExpanded"><Minimize2 v-if="expanded" :size="17"/><Maximize2 v-else :size="17"/></button>
        <button title="关闭预览" aria-label="关闭预览" @click="emit('close')"><X :size="20"/></button>
      </div>
    </header>
    <p v-if="notice" class="preview-notice" role="status">{{ notice }}</p>
    <div v-if="loading" class="preview-placeholder" role="status">正在加载 HTML…</div>
    <div v-else-if="error" class="preview-placeholder preview-error" role="alert"><p>{{ error }}</p><button class="secondary" @click="load">重试</button></div>
    <template v-else>
      <iframe v-show="mode==='preview'" class="html-effect" title="HTML 效果预览" sandbox="allow-scripts" referrerpolicy="no-referrer" :srcdoc="documentSource"></iframe>
      <div v-show="mode==='source'" class="html-source" role="tabpanel" aria-label="HTML 源码" tabindex="0"><pre class="source-numbers" aria-hidden="true"><span v-for="line in lineCount" :key="line">{{ line }}
</span></pre><pre class="source-code"><code class="hljs" v-html="highlighted"></code></pre></div>
    </template>
  </aside>
</template>
