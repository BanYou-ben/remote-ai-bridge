<script setup>
import { reactive, ref, watch } from 'vue'
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
let loadSequence = 0

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
  const sequence = ++loadSequence
  const requestedName = props.name
  loading.value = true
  error.value = null
  profile.value = null
  deleteNotice.value = ''
  saveState.value = 'idle'
  try {
    const loaded = await getProfile(requestedName)
    if (sequence !== loadSequence) return
    profile.value = loaded
    syncForm(loaded, 'idle')
  } catch (caught) {
    if (sequence !== loadSequence) return
    error.value = caught
  } finally {
    if (sequence === loadSequence) loading.value = false
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

watch(() => props.name, load, { immediate: true })
</script>

<template>
  <div class="profile-detail-page">
    <header class="page-header profile-detail-page-header">
      <div>
        <p class="eyebrow">CONNECTION PROFILE</p>
        <h1>配置详情</h1>
        <p>查看安全元数据，并修改后端明确允许更新的连接参数。</p>
      </div>
      <RouterLink class="secondary-button button-link" to="/profiles">← 返回连接配置</RouterLink>
    </header>

    <div v-if="loading" class="profile-detail-loading">
      <span class="connections-loading-mark" aria-hidden="true"></span>
      <div>
        <strong>正在加载配置详情</strong>
        <p>正在读取服务器、代理和 SSH 身份元数据……</p>
      </div>
    </div>

    <div v-else-if="!profile" class="structured-error profile-detail-load-error" role="alert">
      <strong>{{ error?.code || 'PROFILE_LOAD_FAILED' }}</strong>
      <span>{{ error?.message || '配置详情加载失败' }}</span>
    </div>

    <template v-else>
      <div v-if="error" class="structured-error" role="alert">
        <strong>{{ error.code }}</strong>
        <span>{{ error.message }}</span>
      </div>
      <p v-if="deleteNotice" class="security-notice">{{ deleteNotice }}</p>

      <section class="profile-identity-hero">
        <div class="profile-identity-copy">
          <div class="profile-identity-heading">
            <span class="profile-identity-mark" aria-hidden="true"></span>
            <div>
              <p class="panel-kicker">REMOTE CONNECTION</p>
              <h2>{{ profile.name }}</h2>
              <p>{{ profile.ssh_target }}</p>
            </div>
          </div>
          <span class="soft-badge badge-purple">{{ formatProfileType(profile.profile_type) }}</span>
        </div>

        <div class="profile-identity-stats">
          <div>
            <span>SERVER</span>
            <strong>{{ profile.host || profile.ssh_target }}</strong>
          </div>
          <div>
            <span>LOCAL PROXY</span>
            <strong class="monospace">{{ profile.local_proxy_host }}:{{ profile.local_proxy_port }}</strong>
          </div>
          <div>
            <span>REMOTE BIND</span>
            <strong class="monospace">{{ profile.remote_bind_host }}:{{ profile.remote_port }}</strong>
          </div>
          <div>
            <span>AUTO RECONNECT</span>
            <strong>{{ formatBoolean(profile.auto_reconnect, 'enabled') }}</strong>
          </div>
        </div>
      </section>

      <section class="profile-path-panel">
        <div class="connection-path-title">
          <span>CONNECTION PATH</span>
          <strong>Local Proxy → Reverse Tunnel → SSH Host</strong>
        </div>
        <div class="profile-route profile-detail-route">
          <div>
            <span>LOCAL PROXY</span>
            <strong class="monospace">{{ profile.local_proxy_host }}:{{ profile.local_proxy_port }}</strong>
          </div>
          <i aria-hidden="true"></i>
          <div>
            <span>REMOTE BIND</span>
            <strong class="monospace">{{ profile.remote_bind_host }}:{{ profile.remote_port }}</strong>
          </div>
          <i aria-hidden="true"></i>
          <div>
            <span>SSH TARGET</span>
            <strong>{{ profile.ssh_target }}</strong>
          </div>
        </div>
      </section>

      <div class="profile-detail-workspace">
        <section class="profile-metadata-panel">
          <div class="profile-panel-heading">
            <div>
              <p class="panel-kicker">METADATA</p>
              <h2>连接元数据</h2>
              <p>只读身份与服务器信息。</p>
            </div>
          </div>

          <dl class="profile-metadata-list">
            <div><dt>名称</dt><dd>{{ profile.name }}</dd></div>
            <div><dt>配置类型</dt><dd>{{ formatProfileType(profile.profile_type) }}</dd></div>
            <div><dt>主机地址</dt><dd>{{ profile.host || '—' }}</dd></div>
            <div><dt>用户名</dt><dd>{{ profile.username || '—' }}</dd></div>
            <div><dt>SSH 端口</dt><dd>{{ profile.ssh_port || '默认' }}</dd></div>
            <div><dt>SSH 目标</dt><dd>{{ profile.ssh_target }}</dd></div>
          </dl>
        </section>

        <section class="profile-edit-panel">
          <form class="form-grid profile-edit-form" @submit.prevent="save">
            <div class="profile-panel-heading wide">
              <div>
                <p class="panel-kicker">SAFE UPDATE</p>
                <h2>连接参数</h2>
                <p>主机、用户名、SSH 身份和配置名称不可在此修改。</p>
              </div>
              <span
                v-if="saveState !== 'idle'"
                class="save-status"
                :class="{
                  'is-dirty': saveState === 'dirty',
                  'is-saved': saveState === 'saved',
                  'is-error': saveState === 'error',
                }"
                data-test="save-status"
              >
                {{ saveState === 'dirty' ? '有未保存的修改' : saveState === 'saved' ? '✓ 已保存' : saveState === 'error' ? '保存失败' : '' }}
              </span>
            </div>

            <label>
              <span>本地代理端口</span>
              <input v-model.number="form.local_proxy_port" data-test="local-proxy-port" type="number" min="1" max="65535" required :disabled="saving">
            </label>
            <label>
              <span>远端端口</span>
              <input v-model.number="form.remote_port" data-test="remote-port" type="number" min="1" max="65535" required :disabled="saving">
            </label>
            <label class="wide">
              <span>Endpoint Probe URL</span>
              <input v-model.trim="form.endpoint_probe_url" data-test="endpoint-probe-url" type="url" required :disabled="saving">
            </label>
            <label class="checkbox-field wide profile-toggle-field">
              <input v-model="form.auto_reconnect" data-test="auto-reconnect" type="checkbox" :disabled="saving">
              <span>自动重连</span>
            </label>

            <div class="form-actions wide profile-save-actions">
              <button class="primary-button" data-test="save-profile" type="submit" :disabled="saving">
                {{ saving ? '正在保存…' : '保存修改' }}
              </button>
            </div>
          </form>
        </section>
      </div>

      <section class="profile-security-panel">
        <div class="profile-security-intro">
          <span class="security-lock-mark" aria-hidden="true"></span>
          <div>
            <p class="panel-kicker">SECURITY</p>
            <h2>SSH 主机身份</h2>
            <p v-if="profile.profile_type === 'managed'" class="managed-note">
              由 Remote AI Bridge 管理 SSH 凭据。私钥、公钥内容和密码不会在网页中展示。
            </p>
          </div>
        </div>

        <dl>
          <div><dt>主机密钥类型</dt><dd>{{ profile.host_key_type || '—' }}</dd></div>
          <div><dt>Endpoint Probe URL</dt><dd>{{ profile.endpoint_probe_url }}</dd></div>
          <div class="wide"><dt>主机密钥指纹</dt><dd class="monospace">{{ profile.host_key_fingerprint || '—' }}</dd></div>
        </dl>
      </section>

      <div class="danger-zone profile-danger-zone">
        <div>
          <span class="danger-kicker">DANGER ZONE</span>
          <strong>删除连接配置</strong>
          <p>删除行为受后端进程所有权和托管凭据安全规则保护。</p>
        </div>
        <button class="danger-button" type="button" :disabled="deleting" @click="remove">
          {{ deleting ? '正在删除……' : '删除配置' }}
        </button>
      </div>
    </template>
  </div>
</template>
