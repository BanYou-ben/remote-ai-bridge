<script setup>
import StatusBadge from './StatusBadge.vue'
import { formatBoolean, formatRuntimeMessage } from '../utils/display.js'

defineProps({
  runtime: { type: Array, required: true },
})
</script>

<template>
  <p v-if="runtime.length === 0" class="state-message">暂无运行记录</p>
  <div v-else class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>配置</th>
          <th>状态</th>
          <th>托管状态</th>
          <th>进程状态</th>
          <th>远端端口</th>
          <th>说明</th>
          <th>错误代码</th>
          <th>重试</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in runtime" :key="item.profile_name">
          <td><strong class="primary-cell">{{ item.profile_name }}</strong></td>
          <td><StatusBadge :status="item.state" /></td>
          <td>{{ formatBoolean(item.supervised) }}</td>
          <td>{{ formatBoolean(item.process_alive) }}</td>
          <td>{{ item.remote_port ?? '—' }}</td>
          <td class="message-cell">{{ formatRuntimeMessage(item.message) }}</td>
          <td class="monospace">{{ item.error_code || '—' }}</td>
          <td>{{ item.retry_in_seconds == null ? '—' : `${item.retry_in_seconds} 秒` }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
