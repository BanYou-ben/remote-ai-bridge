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

const passCount = computed(() => checks.value.filter((item) => item.check.status === 'PASS').length)
const failCount = computed(() => checks.value.filter((item) => item.check.status === 'FAIL').length)
const skippedCount = computed(() => checks.value.filter((item) => item.check.status === 'SKIP').length)

const summaryLabel = computed(() => {
  if (props.state.loading) return '正在诊断'
  if (props.state.error) return '诊断失败'
  if (failCount.value > 0) return '发现异常'
  if (checks.value.length > 0) return '连接健康'
  return '等待诊断'
})
</script>

<template>
  <div v-if="state.open" class="doctor-backdrop" role="presentation" data-test="doctor-panel">
    <section class="doctor-panel" role="dialog" aria-modal="true" aria-labelledby="doctor-title">
      <header class="doctor-header">
        <div class="doctor-title-copy">
          <p class="panel-kicker">DIAGNOSTICS</p>
          <h2 id="doctor-title">连接诊断</h2>
          <p>{{ state.profileName }}</p>
        </div>
        <button class="close-button" type="button" aria-label="关闭诊断" @click="$emit('close')">×</button>
      </header>

      <section class="doctor-summary" :class="{ 'has-failure': failCount > 0 || state.error }">
        <div class="doctor-summary-status">
          <span class="doctor-summary-mark" aria-hidden="true"></span>
          <div>
            <span>HEALTH</span>
            <strong>{{ summaryLabel }}</strong>
          </div>
        </div>
        <div v-if="!state.loading && !state.error && checks.length" class="doctor-summary-counts">
          <div><strong>{{ passCount }}</strong><span>通过</span></div>
          <div><strong>{{ failCount }}</strong><span>失败</span></div>
          <div><strong>{{ skippedCount }}</strong><span>跳过</span></div>
        </div>
      </section>

      <div class="doctor-body">
        <div v-if="state.loading" class="doctor-loading-state">
          <span class="connections-loading-mark" aria-hidden="true"></span>
          <div>
            <strong>正在执行诊断</strong>
            <p>依次检查 Proxy、SSH、Tunnel、远端监听和 Endpoint。</p>
          </div>
        </div>

        <div v-else-if="state.error" class="doctor-error">
          <span class="doctor-error-mark" aria-hidden="true">!</span>
          <div>
            <strong>诊断失败</strong>
            <p>{{ state.error.message }}</p>
            <p v-if="state.error.code" class="error-code">{{ state.error.code }}</p>
          </div>
        </div>

        <div v-else class="doctor-checks">
          <article v-for="(item, index) in checks" :key="item.section" class="doctor-check">
            <span class="doctor-check-index">{{ String(index + 1).padStart(2, '0') }}</span>
            <div class="doctor-check-main">
              <div class="doctor-check-heading">
                <div>
                  <strong>{{ item.section }}</strong>
                  <span>{{ item.check.name }}</span>
                </div>
                <span class="check-status" :class="`check-${item.check.status.toLowerCase()}`">
                  {{ formatCheckStatus(item.check.status) }}
                </span>
              </div>
              <p>{{ item.check.detail }}</p>
              <dl v-if="item.check.error_code || item.check.http_status">
                <div v-if="item.check.error_code"><dt>错误代码</dt><dd class="monospace">{{ item.check.error_code }}</dd></div>
                <div v-if="item.check.http_status"><dt>HTTP 状态</dt><dd>{{ item.check.http_status }}</dd></div>
              </dl>
            </div>
          </article>
        </div>
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
