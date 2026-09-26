<script setup>
import { computed, onMounted, reactive, ref } from 'vue'

import { getHealth, listProfiles, listRuntime } from '../api/rab.js'
import DoctorPanel from '../components/DoctorPanel.vue'
import ProfileTable from '../components/ProfileTable.vue'
import RuntimeTable from '../components/RuntimeTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { useRuntimeControls } from '../composables/useRuntimeControls.js'

const health = ref(null)
const profiles = ref([])
const runtime = ref([])
const loading = reactive({ health: true, profiles: true, runtime: true })
const errors = reactive({ health: null, profiles: null, runtime: null })

const refreshing = computed(() => Object.values(loading).some(Boolean))
const readyCount = computed(() => runtime.value.filter((item) => item.state === 'READY').length)
const stoppedCount = computed(
  () => runtime.value.filter((item) => ['STOPPED', 'UNSUPERVISED'].includes(item.state)).length,
)

const activeRuntime = computed(() =>
  runtime.value.find((item) =>
    ['READY', 'STARTING', 'CONNECTING', 'DEGRADED', 'STOPPING'].includes(item.state),
  ),
)

const currentProfile = computed(() => {
  if (activeRuntime.value) {
    return profiles.value.find((profile) => profile.name === activeRuntime.value.profile_name) ?? null
  }
  return profiles.value[0] ?? null
})

const currentRuntime = computed(() => {
  if (currentProfile.value) {
    return (
      runtime.value.find((item) => item.profile_name === currentProfile.value.name) ??
      activeRuntime.value ??
      null
    )
  }
  return activeRuntime.value ?? null
})

const currentName = computed(
  () => currentProfile.value?.name ?? currentRuntime.value?.profile_name ?? null,
)

const currentActionState = computed(() => {
  if (!currentProfile.value) return 'idle'
  return controls.actionState[currentProfile.value.name] ?? 'idle'
})

const currentActionError = computed(() => {
  if (!currentProfile.value) return null
  return controls.actionErrors[currentProfile.value.name] ?? null
})

function updateRuntime(snapshot) {
  const index = runtime.value.findIndex((item) => item.profile_name === snapshot.profile_name)
  if (index === -1) runtime.value.push(snapshot)
  else runtime.value.splice(index, 1, snapshot)
}

const controls = useRuntimeControls({ onRuntimeUpdate: updateRuntime })

async function loadSection(key, request, target) {
  loading[key] = true
  errors[key] = null
  try {
    target.value = await request()
  } catch (error) {
    errors[key] = error
    if (key === 'health') target.value = null
  } finally {
    loading[key] = false
  }
}

async function refresh() {
  await Promise.all([
    loadSection('health', getHealth, health),
    loadSection('profiles', listProfiles, profiles),
    loadSection('runtime', listRuntime, runtime),
  ])
}

onMounted(refresh)
</script>

<template>
  <header class="page-header dashboard-page-header">
    <div>
      <p class="eyebrow">OVERVIEW</p>
      <h1>总览</h1>
      <p>查看 Remote AI Bridge 当前连接配置与运行状态</p>
    </div>
    <div class="header-actions">
      <span
        class="header-status"
        :class="loading.health ? 'is-loading' : health ? 'is-online' : 'is-offline'"
      >
        <span class="badge-dot"></span>{{ loading.health ? '正在检查' : health ? '后端在线' : '后端离线' }}
      </span>
      <button class="primary-button" type="button" :disabled="refreshing" @click="refresh">
        {{ refreshing ? '正在刷新……' : '↻ 刷新' }}
      </button>
    </div>
  </header>

  <div class="dashboard-focus-grid">
    <section class="connection-hero" :class="{ 'is-empty': !currentProfile }" aria-labelledby="current-connection-title">
      <div class="connection-hero-top">
        <div>
          <p class="panel-kicker">CURRENT CONNECTION</p>
          <template v-if="currentName">
            <h2 id="current-connection-title">{{ currentName }}</h2>
            <p class="connection-target">
              {{ currentProfile?.ssh_target || '运行记录存在，但对应连接配置不可用' }}
            </p>
          </template>
          <template v-else>
            <h2 id="current-connection-title">建立第一条远程连接</h2>
            <p class="connection-target">配置服务器、SSH 与本地代理后，即可启动 Reverse Tunnel。</p>
          </template>
        </div>

        <StatusBadge v-if="currentRuntime" :status="currentRuntime.state" />
        <span v-else class="status-badge status-inactive">未运行</span>
      </div>

      <template v-if="currentProfile">
        <div class="connection-route" aria-label="Connection path">
          <div class="route-node">
            <span class="route-node-label">LOCAL PROXY</span>
            <strong class="monospace">{{ currentProfile.local_proxy_host }}:{{ currentProfile.local_proxy_port }}</strong>
            <span>Windows 本地代理</span>
          </div>
          <span class="route-link" aria-hidden="true"><i></i></span>
          <div class="route-node route-node-accent">
            <span class="route-node-label">REVERSE TUNNEL</span>
            <strong class="monospace">
              {{ currentProfile.remote_bind_host }}:{{ currentRuntime?.remote_port ?? currentProfile.remote_port }}
            </strong>
            <span>{{ currentRuntime?.supervised ? '已受监督' : '未受监督' }}</span>
          </div>
          <span class="route-link" aria-hidden="true"><i></i></span>
          <div class="route-node">
            <span class="route-node-label">REMOTE HOST</span>
            <strong>{{ currentProfile.ssh_target }}</strong>
            <span>SSH 目标</span>
          </div>
        </div>

        <div class="current-connection-actions">
          <button
            class="connection-primary-action"
            type="button"
            :disabled="currentActionState !== 'idle'"
            @click="controls.connect(currentProfile.name)"
          >
            {{ currentActionState === 'connecting' ? '连接中…' : '连接' }}
          </button>
          <button
            class="connection-secondary-action"
            type="button"
            :disabled="currentActionState !== 'idle'"
            @click="controls.diagnose(currentProfile.name)"
          >
            {{ currentActionState === 'diagnosing' ? '诊断中…' : '运行诊断' }}
          </button>
          <button
            class="connection-secondary-action"
            type="button"
            :disabled="currentActionState !== 'idle'"
            @click="controls.disconnect(currentProfile.name)"
          >
            {{ currentActionState === 'disconnecting' ? '断开中…' : '断开' }}
          </button>
          <RouterLink
            class="connection-manage-link"
            :to="`/profiles/${encodeURIComponent(currentProfile.name)}`"
          >
            管理配置
          </RouterLink>
        </div>

        <div v-if="currentActionError" class="connection-action-error" role="alert">
          <strong>{{ currentActionError.code }}</strong>
          <span>{{ currentActionError.message }}</span>
        </div>
      </template>

      <div v-else-if="!loading.profiles" class="connection-empty">
        <div class="connection-empty-copy">
          <span class="connection-empty-icon" aria-hidden="true">+</span>
          <div>
            <strong>暂无连接配置</strong>
            <span>先添加一台服务器，再从这里直接连接、诊断和管理。</span>
          </div>
        </div>
        <RouterLink class="connection-empty-button" to="/setup">添加连接</RouterLink>
      </div>
    </section>

    <section class="system-snapshot" aria-labelledby="system-snapshot-title">
      <div class="snapshot-heading">
        <div>
          <p class="panel-kicker">SYSTEM STATUS</p>
          <h2 id="system-snapshot-title">系统概览</h2>
        </div>
        <span class="snapshot-live-dot" :class="{ offline: !loading.health && !health }"></span>
      </div>

      <div class="snapshot-list">
        <div class="snapshot-item">
          <div class="snapshot-label">
            <span class="snapshot-icon snapshot-icon-backend" aria-hidden="true"></span>
            <div>
              <strong>后端服务</strong>
              <span v-if="loading.health">正在检查后端服务……</span>
              <span v-else-if="errors.health">后端服务不可用：{{ errors.health.message || '无法连接本地后端' }}</span>
              <span v-else>本地服务运行正常</span>
            </div>
          </div>
          <span class="snapshot-value" :class="{ danger: !loading.health && !health }">
            {{ loading.health ? '检查中' : health ? '在线' : '离线' }}
          </span>
        </div>

        <div class="snapshot-item">
          <div class="snapshot-label">
            <span class="snapshot-icon snapshot-icon-profile" aria-hidden="true"></span>
            <div>
              <strong>连接配置</strong>
              <span v-if="loading.profiles">正在加载连接配置……</span>
              <span v-else-if="errors.profiles">{{ errors.profiles.message || '连接配置加载失败' }}</span>
              <span v-else>已保存配置</span>
            </div>
          </div>
          <span class="snapshot-number">{{ loading.profiles ? '—' : profiles.length }}</span>
        </div>

        <div class="snapshot-item">
          <div class="snapshot-label">
            <span class="snapshot-icon snapshot-icon-runtime" aria-hidden="true"></span>
            <div>
              <strong>运行状态</strong>
              <span v-if="loading.runtime">正在加载运行状态……</span>
              <span v-else-if="errors.runtime">{{ errors.runtime.message || '运行状态加载失败' }}</span>
              <span v-else>{{ readyCount }} 已就绪 · {{ stoppedCount }} 未运行</span>
            </div>
          </div>
          <span class="snapshot-number">{{ loading.runtime ? '—' : readyCount }}</span>
        </div>
      </div>
    </section>
  </div>

  <section class="section-card dashboard-section" aria-labelledby="profiles-title">
    <div class="section-heading">
      <div>
        <p class="section-kicker">CONNECTIONS</p>
        <h2 id="profiles-title">连接配置</h2>
        <p>当前保存的远程连接</p>
      </div>
      <div class="section-heading-actions">
        <span class="count">{{ profiles.length }} 条</span>
        <RouterLink class="section-add-link" to="/setup">+ 添加连接</RouterLink>
      </div>
    </div>

    <p v-if="loading.profiles" class="state-message">正在加载连接配置……</p>
    <div v-else-if="errors.profiles" class="structured-error" role="alert">
      <strong>{{ errors.profiles.code || 'PROFILE_LIST_FAILED' }}</strong>
      <span>{{ errors.profiles.message || '连接配置加载失败' }}</span>
    </div>
    <div v-else-if="profiles.length === 0" class="dashboard-empty-state">
      <div class="dashboard-empty-visual" aria-hidden="true">
        <span></span><i></i><span></span>
      </div>
      <div>
        <strong>暂无连接配置</strong>
        <p>添加服务器后，可通过 SSH Reverse Tunnel 将远端应用连接到本地代理。</p>
      </div>
      <RouterLink class="primary-button button-link" to="/setup">添加第一个连接</RouterLink>
    </div>
    <ProfileTable
      v-else
      :profiles="profiles"
      :action-state="controls.actionState"
      :action-errors="controls.actionErrors"
      @connect="controls.connect"
      @disconnect="controls.disconnect"
      @doctor="controls.diagnose"
    />
  </section>

  <section class="section-card dashboard-section" aria-labelledby="runtime-title">
    <div class="section-heading">
      <div>
        <p class="section-kicker">RUNTIME</p>
        <h2 id="runtime-title">运行状态</h2>
        <p>连接进程与 Reverse Tunnel 状态</p>
      </div>
      <span class="count">{{ runtime.length }} 条</span>
    </div>
    <p v-if="loading.runtime" class="state-message">正在加载运行状态……</p>
    <div v-else-if="errors.runtime" class="structured-error" role="alert">
      <strong>{{ errors.runtime.code || 'RUNTIME_LIST_FAILED' }}</strong>
      <span>{{ errors.runtime.message || '运行状态加载失败' }}</span>
    </div>
    <RuntimeTable v-else :runtime="runtime" />
  </section>

  <DoctorPanel
    :state="controls.doctor"
    @close="controls.closeDoctor"
    @retry="controls.diagnose"
  />
</template>
