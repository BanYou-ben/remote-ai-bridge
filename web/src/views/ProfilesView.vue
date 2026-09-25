<script setup>
import { computed, onMounted, ref } from 'vue'

import { listProfiles } from '../api/rab.js'
import { formatBoolean, formatProfileType } from '../utils/display.js'

const profiles = ref([])
const loading = ref(true)
const error = ref(null)
const selectedName = ref(null)

const selectedProfile = computed(() =>
  profiles.value.find((profile) => profile.name === selectedName.value) ?? profiles.value[0] ?? null,
)

const managedCount = computed(
  () => profiles.value.filter((profile) => profile.profile_type === 'managed').length,
)

const autoReconnectCount = computed(
  () => profiles.value.filter((profile) => profile.auto_reconnect).length,
)

async function loadProfiles() {
  loading.value = true
  error.value = null
  try {
    profiles.value = await listProfiles()
    if (!profiles.value.some((profile) => profile.name === selectedName.value)) {
      selectedName.value = profiles.value[0]?.name ?? null
    }
  } catch (caught) {
    error.value = caught
  } finally {
    loading.value = false
  }
}

function selectProfile(name) {
  selectedName.value = name
}

onMounted(loadProfiles)
</script>

<template>
  <header class="page-header connections-page-header">
    <div>
      <p class="eyebrow">CONNECTIONS</p>
      <h1>连接配置</h1>
      <p>管理 Remote AI Bridge 的服务器、代理与 Reverse Tunnel 配置；凭据信息不会在此显示。</p>
    </div>
    <div class="header-actions">
      <RouterLink class="primary-button button-link" to="/setup">+ 添加连接</RouterLink>
      <button class="secondary-button" type="button" :disabled="loading" @click="loadProfiles">刷新</button>
    </div>
  </header>

  <div v-if="loading" class="connections-loading-state">
    <span class="connections-loading-mark" aria-hidden="true"></span>
    <div>
      <strong>正在加载连接配置</strong>
      <p>正在从本地后端读取服务器与隧道配置……</p>
    </div>
  </div>

  <div v-else-if="error" class="structured-error connections-error-state" role="alert">
    <strong>{{ error.code || 'PROFILE_LIST_FAILED' }}</strong>
    <span>{{ error.message || '连接配置加载失败' }}</span>
  </div>

  <section v-else-if="profiles.length === 0" class="connections-empty-workspace">
    <div class="connections-empty-graphic" aria-hidden="true">
      <span class="empty-node empty-node-left"></span>
      <span class="empty-bridge"></span>
      <span class="empty-node empty-node-right"></span>
      <i></i>
    </div>
    <p class="panel-kicker">NO CONNECTIONS</p>
    <h2>还没有连接配置</h2>
    <p>
      添加服务器后，即可通过 SSH Reverse Tunnel 将远端应用连接到本地代理，
      并在这里统一管理端口、自动重连和安全元数据。
    </p>
    <RouterLink class="primary-button button-link" to="/setup">+ 添加第一个连接</RouterLink>

    <div class="empty-capabilities">
      <div>
        <span>01</span>
        <strong>Verify Host</strong>
        <p>确认 SSH 主机指纹，避免连接到错误服务器。</p>
      </div>
      <div>
        <span>02</span>
        <strong>Discover Proxy</strong>
        <p>检测本地代理并保存明确的连接参数。</p>
      </div>
      <div>
        <span>03</span>
        <strong>Run Tunnel</strong>
        <p>通过 Reverse Tunnel 暴露远端 loopback 端点。</p>
      </div>
    </div>
  </section>

  <template v-else>
    <div class="connections-summary">
      <div>
        <span class="summary-label">TOTAL</span>
        <strong>{{ profiles.length }}</strong>
        <p>连接配置</p>
      </div>
      <div>
        <span class="summary-label">MANAGED</span>
        <strong>{{ managedCount }}</strong>
        <p>托管配置</p>
      </div>
      <div>
        <span class="summary-label">AUTO RECONNECT</span>
        <strong>{{ autoReconnectCount }}</strong>
        <p>已开启</p>
      </div>
      <div class="connections-summary-note">
        <span class="summary-status-dot" aria-hidden="true"></span>
        <p>敏感凭据不会显示在网页中</p>
      </div>
    </div>

    <section class="connections-workspace" aria-label="Connection workspace">
      <aside class="connection-list-panel">
        <div class="connection-list-heading">
          <div>
            <p class="panel-kicker">SAVED CONNECTIONS</p>
            <h2>服务器连接</h2>
          </div>
          <span class="count">{{ profiles.length }} 条</span>
        </div>

        <div class="connection-list">
          <button
            v-for="profile in profiles"
            :key="profile.name"
            class="connection-list-item"
            :class="{ active: selectedProfile?.name === profile.name }"
            type="button"
            @click="selectProfile(profile.name)"
          >
            <span class="connection-list-state" :class="{ managed: profile.profile_type === 'managed' }" aria-hidden="true"></span>
            <span class="connection-list-copy">
              <strong>{{ profile.name }}</strong>
              <span>{{ profile.ssh_target }}</span>
            </span>
            <span class="connection-list-arrow" aria-hidden="true">›</span>
          </button>
        </div>

        <RouterLink class="connection-list-add" to="/setup">
          <span aria-hidden="true">+</span>
          添加连接
        </RouterLink>
      </aside>

      <article v-if="selectedProfile" class="connection-detail-panel">
        <header class="connection-detail-header">
          <div>
            <p class="panel-kicker">CONNECTION PROFILE</p>
            <h2>{{ selectedProfile.name }}</h2>
            <p>{{ selectedProfile.ssh_target }}</p>
          </div>
          <span class="soft-badge badge-purple">{{ formatProfileType(selectedProfile.profile_type) }}</span>
        </header>

        <div class="connection-path-card">
          <div class="connection-path-title">
            <span>CONNECTION PATH</span>
            <strong>本地代理 → Reverse Tunnel → 远端主机</strong>
          </div>
          <div class="profile-route">
            <div>
              <span>LOCAL PROXY</span>
              <strong class="monospace">{{ selectedProfile.local_proxy_host }}:{{ selectedProfile.local_proxy_port }}</strong>
            </div>
            <i aria-hidden="true"></i>
            <div>
              <span>REMOTE BIND</span>
              <strong class="monospace">{{ selectedProfile.remote_bind_host }}:{{ selectedProfile.remote_port }}</strong>
            </div>
            <i aria-hidden="true"></i>
            <div>
              <span>SSH TARGET</span>
              <strong>{{ selectedProfile.ssh_target }}</strong>
            </div>
          </div>
        </div>

        <div class="connection-detail-grid">
          <section>
            <div class="detail-section-heading">
              <span>SERVER</span>
              <strong>服务器</strong>
            </div>
            <dl>
              <div><dt>主机地址</dt><dd>{{ selectedProfile.host || selectedProfile.ssh_target }}</dd></div>
              <div><dt>用户名</dt><dd>{{ selectedProfile.username || '—' }}</dd></div>
              <div><dt>SSH 端口</dt><dd>{{ selectedProfile.ssh_port || '默认' }}</dd></div>
            </dl>
          </section>

          <section>
            <div class="detail-section-heading">
              <span>BEHAVIOR</span>
              <strong>连接行为</strong>
            </div>
            <dl>
              <div>
                <dt>自动重连</dt>
                <dd>
                  <span class="soft-badge badge-success">
                    <span class="badge-dot"></span>{{ formatBoolean(selectedProfile.auto_reconnect, 'enabled') }}
                  </span>
                </dd>
              </div>
              <div><dt>配置类型</dt><dd>{{ formatProfileType(selectedProfile.profile_type) }}</dd></div>
              <div><dt>主机密钥类型</dt><dd>{{ selectedProfile.host_key_type || '旧版配置' }}</dd></div>
            </dl>
          </section>
        </div>

        <section class="connection-security-panel">
          <div class="connection-security-heading">
            <div>
              <p class="panel-kicker">SECURITY</p>
              <h3>SSH 主机身份</h3>
            </div>
            <span class="security-lock-mark" aria-hidden="true"></span>
          </div>
          <dl>
            <div>
              <dt>主机密钥类型</dt>
              <dd>{{ selectedProfile.host_key_type || '旧版配置' }}</dd>
            </div>
            <div class="wide">
              <dt>主机密钥指纹</dt>
              <dd class="monospace">{{ selectedProfile.host_key_fingerprint || '—' }}</dd>
            </div>
          </dl>
          <p>仅展示可安全公开的主机身份元数据；SSH 私钥、公钥内容和密码不会在此页面返回。</p>
        </section>

        <footer class="connection-detail-actions">
          <div>
            <span class="detail-action-hint">需要调整端口、自动重连或 Endpoint Probe？</span>
          </div>
          <RouterLink
            class="primary-button button-link"
            :to="`/profiles/${encodeURIComponent(selectedProfile.name)}`"
          >
            管理配置
          </RouterLink>
        </footer>
      </article>
    </section>
  </template>
</template>
