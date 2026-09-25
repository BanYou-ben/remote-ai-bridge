<script setup>
defineProps({
  health: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  error: { type: Object, default: null },
})
</script>

<template>
  <section class="overview-card overview-health" aria-labelledby="backend-health-title">
    <div class="overview-card-heading">
      <div class="overview-label">
        <span class="accent-dot accent-green" aria-hidden="true"></span>
        <h2 id="backend-health-title">后端服务</h2>
      </div>
      <span v-if="health" class="health-indicator online">在线</span>
      <span v-else-if="error" class="health-indicator offline">离线</span>
    </div>
    <p v-if="loading" class="state-message">正在检查后端服务……</p>
    <div v-else-if="error" class="state-message error-message">
      <strong>{{ error.code || 'BACKEND_UNAVAILABLE' }}</strong>
      <span>后端服务不可用：{{ error.message || '无法连接本地后端' }}</span>
    </div>
    <div v-else-if="health" class="overview-value">
      <strong class="overview-brand">Remote AI Bridge</strong>
      <span>本地服务运行正常</span>
    </div>
  </section>
</template>
