<script setup>
import { onMounted, ref } from 'vue'

import { listProfiles } from '../api/rab.js'
import { formatBoolean, formatProfileType } from '../utils/display.js'

const profiles = ref([])
const loading = ref(true)
const error = ref(null)

async function loadProfiles() {
  loading.value = true
  error.value = null
  try {
    profiles.value = await listProfiles()
  } catch (caught) {
    error.value = caught
  } finally {
    loading.value = false
  }
}

onMounted(loadProfiles)
</script>

<template>
  <header class="page-header">
    <div>
      <p class="eyebrow">配置</p>
      <h1>连接配置</h1>
      <p>查看托管和旧版连接配置；凭据信息不会在此显示。</p>
    </div>
    <div class="header-actions">
      <RouterLink class="primary-button button-link" to="/setup">添加连接</RouterLink>
      <button class="secondary-button" type="button" :disabled="loading" @click="loadProfiles">刷新</button>
    </div>
  </header>

  <section class="section-card">
    <p v-if="loading" class="state-message">正在加载连接配置……</p>
    <div v-else-if="error" class="structured-error" role="alert">
      <strong>{{ error.code || 'PROFILE_LIST_FAILED' }}</strong>
      <span>{{ error.message || '连接配置加载失败' }}</span>
    </div>
    <p v-else-if="profiles.length === 0" class="state-message">暂无连接配置</p>
    <div v-else class="profile-grid">
      <article v-for="profile in profiles" :key="profile.name" class="profile-card">
        <div class="profile-card-heading">
          <div>
            <h2><RouterLink :to="`/profiles/${encodeURIComponent(profile.name)}`">{{ profile.name }}</RouterLink></h2>
            <p>{{ profile.ssh_target }}</p>
          </div>
          <span class="soft-badge badge-purple">{{ formatProfileType(profile.profile_type) }}</span>
        </div>
        <dl>
          <div><dt>主机地址</dt><dd>{{ profile.host || profile.ssh_target }}</dd></div>
          <div><dt>用户名</dt><dd>{{ profile.username || '—' }}</dd></div>
          <div><dt>SSH 端口</dt><dd>{{ profile.ssh_port || '默认' }}</dd></div>
          <div><dt>本地代理</dt><dd>{{ profile.local_proxy_host }}:{{ profile.local_proxy_port }}</dd></div>
          <div><dt>远端端口</dt><dd>{{ profile.remote_bind_host }}:{{ profile.remote_port }}</dd></div>
          <div><dt>自动重连</dt><dd><span class="soft-badge badge-success"><span class="badge-dot"></span>{{ formatBoolean(profile.auto_reconnect, 'enabled') }}</span></dd></div>
          <div><dt>主机密钥类型</dt><dd>{{ profile.host_key_type || '旧版配置' }}</dd></div>
          <div class="wide"><dt>主机密钥指纹</dt><dd class="monospace">{{ profile.host_key_fingerprint || '—' }}</dd></div>
        </dl>
      </article>
    </div>
  </section>
</template>
