<script setup>
import { formatBoolean, formatProfileType } from '../utils/display.js'

defineProps({
  profiles: { type: Array, required: true },
})
</script>

<template>
  <p v-if="profiles.length === 0" class="state-message">暂无连接配置</p>
  <div v-else class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>名称</th>
          <th>SSH 目标</th>
          <th>本地代理</th>
          <th>远端端点</th>
          <th>自动重连</th>
          <th>配置类型</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="profile in profiles" :key="profile.name">
          <td><strong class="primary-cell">{{ profile.name }}</strong></td>
          <td>{{ profile.ssh_target }}</td>
          <td class="monospace">{{ profile.local_proxy_host }}:{{ profile.local_proxy_port }}</td>
          <td class="monospace">{{ profile.remote_bind_host }}:{{ profile.remote_port }}</td>
          <td><span class="soft-badge badge-success"><span class="badge-dot"></span>{{ formatBoolean(profile.auto_reconnect, 'enabled') }}</span></td>
          <td><span class="soft-badge badge-neutral">{{ formatProfileType(profile.profile_type) }}</span></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
