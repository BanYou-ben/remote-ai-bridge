<script setup>
import { computed, onMounted, reactive, ref } from 'vue'

import { getHealth, listProfiles, listRuntime } from '../api/rab.js'
import BackendHealthCard from '../components/BackendHealthCard.vue'
import ProfileTable from '../components/ProfileTable.vue'
import RuntimeTable from '../components/RuntimeTable.vue'

const health = ref(null)
const profiles = ref([])
const runtime = ref([])
const loading = reactive({ health: true, profiles: true, runtime: true })
const errors = reactive({ health: null, profiles: null, runtime: null })
const refreshing = computed(() => Object.values(loading).some(Boolean))
const readyCount = computed(() => runtime.value.filter((item) => item.state === 'READY').length)
const unsupervisedCount = computed(() => runtime.value.filter((item) => item.state === 'UNSUPERVISED').length)

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
  <header class="page-header">
    <div>
      <p class="eyebrow">总览</p>
      <h1>总览</h1>
      <p>查看 Remote AI Bridge 当前连接配置与运行状态</p>
    </div>
    <div class="header-actions">
      <span class="header-status" :class="loading.health ? 'is-loading' : health ? 'is-online' : 'is-offline'">
        <span class="badge-dot"></span>{{ loading.health ? '正在检查' : health ? '后端在线' : '后端离线' }}
      </span>
      <button class="primary-button" type="button" :disabled="refreshing" @click="refresh">
        {{ refreshing ? '正在刷新……' : '↻ 刷新' }}
      </button>
    </div>
  </header>

  <div class="overview-grid">
    <BackendHealthCard :health="health" :loading="loading.health" :error="errors.health" />
    <section class="overview-card" aria-labelledby="profile-overview-title">
      <div class="overview-card-heading">
        <div class="overview-label">
          <span class="accent-dot accent-purple" aria-hidden="true"></span>
          <h2 id="profile-overview-title">连接配置</h2>
        </div>
      </div>
      <div class="overview-value">
        <strong class="metric-value">{{ loading.profiles ? '—' : profiles.length }}</strong>
        <span>已保存配置</span>
      </div>
    </section>
    <section class="overview-card" aria-labelledby="runtime-overview-title">
      <div class="overview-card-heading">
        <div class="overview-label">
          <span class="accent-dot accent-blue" aria-hidden="true"></span>
          <h2 id="runtime-overview-title">运行状态</h2>
        </div>
      </div>
      <div class="runtime-metrics">
        <div><strong>{{ loading.runtime ? '—' : readyCount }}</strong><span>已就绪</span></div>
        <div><strong>{{ loading.runtime ? '—' : unsupervisedCount }}</strong><span>未托管</span></div>
      </div>
    </section>
  </div>

  <section class="section-card" aria-labelledby="profiles-title">
    <div class="section-heading">
      <div><h2 id="profiles-title">连接配置</h2><p>当前保存的远程连接</p></div>
      <span class="count">{{ profiles.length }} 条</span>
    </div>
    <p v-if="loading.profiles" class="state-message">正在加载连接配置……</p>
    <p v-else-if="errors.profiles" class="state-message error-message">连接配置加载失败</p>
    <ProfileTable v-else :profiles="profiles" />
  </section>

  <section class="section-card" aria-labelledby="runtime-title">
    <div class="section-heading">
      <div><h2 id="runtime-title">运行状态</h2><p>连接进程与隧道状态</p></div>
      <span class="count">{{ runtime.length }} 条</span>
    </div>
    <p v-if="loading.runtime" class="state-message">正在加载运行状态……</p>
    <p v-else-if="errors.runtime" class="state-message error-message">运行状态加载失败</p>
    <RuntimeTable v-else :runtime="runtime" />
  </section>
</template>
