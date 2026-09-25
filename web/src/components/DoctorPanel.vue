<script setup>
import { computed } from 'vue'

import { formatCheckStatus } from '../utils/display.js'

const props = defineProps({
  state: { type: Object, required: true },
})

defineEmits(['close', 'retry'])

const checks = computed(() => {
  const report = props.state.report
  if (!report) return []
  return [
    { section: '本地代理', check: report.local?.tcp },
    { section: 'HTTP 代理握手', check: report.local?.handshake },
    { section: '外部端点', check: report.local?.endpoint },
    { section: 'SSH', check: report.ssh },
    { section: '隧道进程', check: report.tunnel },
    { section: '远端监听', check: report.remote_listener },
    { section: '远端端点', check: report.remote_endpoint },
  ].filter((item) => item.check)
})
</script>

<template>
  <div v-if="state.open" class="doctor-backdrop" role="presentation" data-test="doctor-panel">
    <section class="doctor-panel" role="dialog" aria-modal="true" aria-labelledby="doctor-title">
      <header class="doctor-header">
        <div>
          <h2 id="doctor-title">连接诊断</h2>
          <p>{{ state.profileName }}</p>
        </div>
        <button class="close-button" type="button" aria-label="关闭诊断" @click="$emit('close')">×</button>
      </header>

      <p v-if="state.loading" class="state-message">正在执行诊断……</p>
      <div v-else-if="state.error" class="doctor-error">
        <strong>诊断失败</strong>
        <p>{{ state.error.message }}</p>
        <p v-if="state.error.code" class="error-code">{{ state.error.code }}</p>
      </div>
      <div v-else class="doctor-checks">
        <article v-for="item in checks" :key="item.section" class="doctor-check">
          <div class="doctor-check-heading">
            <div><strong>{{ item.section }}</strong><span>{{ item.check.name }}</span></div>
            <span class="check-status" :class="`check-${item.check.status.toLowerCase()}`">
              {{ formatCheckStatus(item.check.status) }}
            </span>
          </div>
          <p>{{ item.check.detail }}</p>
          <dl v-if="item.check.error_code || item.check.http_status">
            <div v-if="item.check.error_code"><dt>错误代码</dt><dd>{{ item.check.error_code }}</dd></div>
            <div v-if="item.check.http_status"><dt>HTTP 状态</dt><dd>{{ item.check.http_status }}</dd></div>
          </dl>
        </article>
      </div>

      <footer class="doctor-actions">
        <button class="secondary-button" type="button" @click="$emit('close')">关闭</button>
        <button class="primary-button" type="button" :disabled="state.loading" @click="$emit('retry', state.profileName)">
          重新诊断
        </button>
      </footer>
    </section>
  </div>
</template>
