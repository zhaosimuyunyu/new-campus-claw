<script setup lang="ts">
import { computed, ref } from 'vue'
import { Code2, Download, Eye, FileCode2 } from 'lucide-vue-next'
import { download, openProtected, type AgentMessage } from './api'
import { downloadHtml, messagePresentation, renderMarkdown, type HtmlFile } from './htmlArtifacts'

const props = defineProps<{message:AgentMessage}>()
const emit = defineEmits<{open:[file:HtmlFile]}>()
const presentation = computed(() => messagePresentation(props.message))
const error = ref('')
const usedTools = computed(()=>(props.message.metadata.tools||[]) as {name:string;status:string}[])
async function save(file:HtmlFile) {
  error.value = ''
  try {
    if (file.artifact) await download({...file.artifact, size:0})
    else downloadHtml(file, file.source || '')
  } catch (e) { error.value = e instanceof Error ? e.message : '下载失败，请重试' }
}
async function otherAction(url:string, name?:string) {
  error.value = ''
  try {
    if (name) await download({id:0, name, size:0, created_at:'', download_url:url})
    else await openProtected(url)
  } catch (e) { error.value = e instanceof Error ? e.message : '文件操作失败' }
}
</script>

<template>
  <div v-if="usedTools.length" class="used-tools"><span v-for="(tool,index) in usedTools" :key="index">{{tool.status==='ok'?'✓':'!'}} {{tool.name}}<small v-if="tool.status!=='ok'"> · 未成功</small></span></div>
  <div v-if="presentation.display" class="markdown-body" v-html="renderMarkdown(presentation.display)"></div>
  <div v-for="file in presentation.files" :key="file.key" class="html-file-card">
    <button class="html-file-open" @click="emit('open', file)">
      <span class="html-file-icon"><FileCode2 :size="24"/></span>
      <span><strong>{{ file.name }}</strong><small>代码 · HTML</small></span>
    </button>
    <button class="secondary" :aria-label="`下载 ${file.name}`" @click="save(file)"><Download :size="15"/> 下载</button>
  </div>
  <div v-for="file in presentation.others" :key="file.id" class="message-artifacts">
    <button @click="otherAction(file.mime_type.startsWith('image/')?file.download_url:file.preview_url)"><Eye :size="14"/> {{ file.name }}</button>
    <button @click="otherAction(file.download_url, file.name)"><Download :size="14"/> 下载</button>
  </div>
  <small v-if="message.metadata.partial" class="muted"><Code2 :size="12"/> 回复未完成，HTML 尚未保存为文件</small>
  <p v-if="error" class="preview-error" role="alert">{{ error }}</p>
</template>

<style scoped>
.used-tools { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
.used-tools span { padding: 4px 9px; background: var(--red-50); border: 1px solid var(--red-100); border-radius: 999px; font-size: 11px; color: var(--red-600); }
.used-tools small { color: var(--warn); }
</style>
