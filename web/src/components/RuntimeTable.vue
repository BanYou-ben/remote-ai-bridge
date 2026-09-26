<script setup>
import StatusBadge from './StatusBadge.vue'
import { formatBoolean, formatRuntimeMessage, formatSupervision } from '../utils/display.js'

defineProps({
  runtime: { type: Array, required: true },
})
</script>

<template>
  <div v-if="runtime.length === 0" class="runtime-empty-state">
    <span class="runtime-empty-mark" aria-hidden="true"></span>
    <div>
      <strong>暂无运行记录</strong>
      <p>建立连接后，这里会展示 supervisor、进程、远端监听与重试状态。</p>
    </div>
  </div>

  <div v-else class="runtime-list">
    <article v-for="item in runtime" :key="item.profile_name" class="runtime-item">
      <div class="runtime-item-primary">
        <span class="runtime-state-rail" :class="`state-${String(item.state || 'unknown').toLowerCase()}`" aria-hidden="true"></span>
        <div>
          <strong>{{ item.profile_name }}</strong>
          <p>{{ formatRuntimeMessage(item.message) }}</p>
        </div>
      </div>

      <div class="runtime-item-status">
        <span class="runtime-field-label">STATUS</span>
        <StatusBadge :status="item.state" />
      </div>

      <div class="runtime-item-field">
        <span class="runtime-field-label">SUPERVISION</span>
        <strong>{{ formatSupervision(item.supervised) }}</strong>
      </div>

      <div class="runtime-item-field">
        <span class="runtime-field-label">PROCESS</span>
        <strong>{{ formatBoolean(item.process_alive) }}</strong>
      </div>

      <div class="runtime-item-field">
        <span class="runtime-field-label">REMOTE PORT</span>
        <strong class="monospace">{{ item.remote_port ?? '—' }}</strong>
      </div>

      <div class="runtime-item-technical">
        <span v-if="item.error_code" class="runtime-error-code monospace">{{ item.error_code }}</span>
        <span v-if="item.retry_in_seconds != null" class="runtime-retry">
          {{ item.retry_in_seconds }} 秒后重试
        </span>
        <span v-if="!item.error_code && item.retry_in_seconds == null">无待处理错误</span>
      </div>
    </article>
  </div>
</template>
