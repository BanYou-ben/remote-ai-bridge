<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { deleteProfile, getProfile, updateProfile } from '../api/rab.js'
import { formatBoolean, formatProfileType } from '../utils/display.js'

const props = defineProps({ name: { type: String, required: true } })
const router = useRouter()
const profile = ref(null)
const loading = ref(true)
const saving = ref(false)
const saveState = ref('idle')
const deleting = ref(false)
const error = ref(null)
const deleteNotice = ref('')
const form = reactive({ local_proxy_port: 0, remote_port: 0, auto_reconnect: true, endpoint_probe_url: '' })
let syncingForm = false

function syncForm(value, nextState) {
  syncingForm = true
  try {
    form.local_proxy_port = value.local_proxy_port
    form.remote_port = value.remote_port
    form.auto_reconnect = value.auto_reconnect
    form.endpoint_probe_url = value.endpoint_probe_url
  } finally {
    syncingForm = false
  }
  saveState.value = nextState
}

watch(
  () => [form.local_proxy_port, form.remote_port, form.auto_reconnect, form.endpoint_probe_url],
  () => {
    if (syncingForm || saving.value) return
    saveState.value = 'dirty'
    error.value = null
  },
  { flush: 'sync' },
)

async function load() {
  loading.value = true
  error.value = null
  try {
    profile.value = await getProfile(props.name)
    syncForm(profile.value, 'idle')
  } catch (caught) {
    error.value = caught
  } finally {
    loading.value = false
  }
}

async function save() {
  if (saving.value) return
  saving.value = true
  saveState.value = 'saving'
  error.value = null
  try {
    profile.value = await updateProfile(props.name, {
      local_proxy_port: Number(form.local_proxy_port),
      remote_port: Number(form.remote_port),
      auto_reconnect: Boolean(form.auto_reconnect),
      endpoint_probe_url: form.endpoint_probe_url,
    })
    syncForm(profile.value, 'saved')
  } catch (caught) {
    error.value = caught
    saveState.value = 'error'
  } finally {
    saving.value = false
  }
}

async function remove() {
  if (!window.confirm(`确定删除连接配置 ${props.name} 吗？`)) return
  deleting.value = true
  error.value = null
  deleteNotice.value = ''
  try {
    await deleteProfile(props.name)
    await router.push('/profiles')
  } catch (caught) {
    error.value = caught
    if (caught.code === 'MANAGED_PROFILE_CREDENTIAL_CLEANUP_REQUIRED') {
      deleteNotice.value = '该配置包含由 RAB 管理的 SSH 凭据，需要先完成安全凭据清理，当前网页暂不支持直接删除。'
    }
  } finally {
    deleting.value = false
  }
}

onMounted(load)
</script>

<template>
  <header class="page-header">
    <div><p class="eyebrow">连接配置</p><h1>配置详情</h1><p>查看连接元数据并修改后端明确支持的安全字段。</p></div>
    <RouterLink class="secondary-button button-link" to="/profiles">返回连接配置</RouterLink>
  </header>

  <section class="section-card">
    <p v-if="loading" class="state-message">正在加载配置详情……</p>
    <div v-else-if="!profile" class="structured-error" role="alert"><strong>{{ error?.code || 'PROFILE_LOAD_FAILED' }}</strong><span>{{ error?.message || '配置详情加载失败' }}</span></div>
    <template v-else>
      <div v-if="error" class="structured-error" role="alert"><strong>{{ error.code }}</strong><span>{{ error.message }}</span></div>
      <p v-if="deleteNotice" class="security-notice">{{ deleteNotice }}</p>
      <div class="section-heading"><div><h2>{{ profile.name }}</h2><p>{{ formatProfileType(profile.profile_type) }}连接配置</p></div></div>
      <dl class="detail-grid">
        <div><dt>名称</dt><dd>{{ profile.name }}</dd></div>
        <div><dt>配置类型</dt><dd>{{ formatProfileType(profile.profile_type) }}</dd></div>
        <div><dt>主机地址</dt><dd>{{ profile.host || '—' }}</dd></div>
        <div><dt>用户名</dt><dd>{{ profile.username || '—' }}</dd></div>
        <div><dt>SSH 端口</dt><dd>{{ profile.ssh_port || '默认' }}</dd></div>
        <div><dt>SSH 目标</dt><dd>{{ profile.ssh_target }}</dd></div>
        <div><dt>本地代理</dt><dd>{{ profile.local_proxy_host }}:{{ profile.local_proxy_port }}</dd></div>
        <div><dt>远端端口</dt><dd>{{ profile.remote_bind_host }}:{{ profile.remote_port }}</dd></div>
        <div><dt>自动重连</dt><dd>{{ formatBoolean(profile.auto_reconnect, 'enabled') }}</dd></div>
        <div class="wide"><dt>Endpoint Probe URL</dt><dd>{{ profile.endpoint_probe_url }}</dd></div>
        <div><dt>主机密钥类型</dt><dd>{{ profile.host_key_type || '—' }}</dd></div>
        <div class="wide"><dt>主机密钥指纹</dt><dd class="monospace">{{ profile.host_key_fingerprint || '—' }}</dd></div>
      </dl>
      <p v-if="profile.profile_type === 'managed'" class="managed-note">由 Remote AI Bridge 管理 SSH 凭据。私钥、公钥内容和密码不会在网页中展示。</p>

      <form class="form-grid profile-edit-form" @submit.prevent="save">
        <div class="section-heading wide"><div><h2>安全更新</h2><p>主机、用户名、SSH 身份和配置名称不可在此修改。</p></div></div>
        <label>本地代理端口<input v-model.number="form.local_proxy_port" data-test="local-proxy-port" type="number" min="1" max="65535" required :disabled="saving"></label>
        <label>远端端口<input v-model.number="form.remote_port" data-test="remote-port" type="number" min="1" max="65535" required :disabled="saving"></label>
        <label class="wide">Endpoint Probe URL<input v-model.trim="form.endpoint_probe_url" data-test="endpoint-probe-url" type="url" required :disabled="saving"></label>
        <label class="checkbox-field wide"><input v-model="form.auto_reconnect" data-test="auto-reconnect" type="checkbox" :disabled="saving"> 自动重连</label>
        <div class="form-actions wide">
          <button class="primary-button" data-test="save-profile" type="submit" :disabled="saving">{{ saving ? '正在保存…' : '保存修改' }}</button>
          <span v-if="saveState === 'dirty'" class="save-status is-dirty" data-test="save-status">有未保存的修改</span>
          <span v-else-if="saveState === 'saved'" class="save-status is-saved" data-test="save-status">✓ 已保存</span>
          <span v-else-if="saveState === 'error'" class="save-status is-error" data-test="save-status">保存失败</span>
        </div>
      </form>

      <div class="danger-zone">
        <div><strong>删除连接配置</strong><p>删除行为受后端进程所有权和托管凭据安全规则保护。</p></div>
        <button class="danger-button" type="button" :disabled="deleting" @click="remove">{{ deleting ? '正在删除……' : '删除配置' }}</button>
      </div>
    </template>
  </section>
</template>
