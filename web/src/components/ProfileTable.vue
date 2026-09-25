<script setup>
import { formatBoolean, formatProfileType } from '../utils/display.js'

defineProps({
  profiles: { type: Array, required: true },
  actionState: { type: Object, default: () => ({}) },
  actionErrors: { type: Object, default: () => ({}) },
})

defineEmits(['connect', 'disconnect', 'doctor'])
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
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="profile in profiles" :key="profile.name">
          <tr>
            <td><strong class="primary-cell">{{ profile.name }}</strong></td>
            <td>{{ profile.ssh_target }}</td>
            <td class="monospace">{{ profile.local_proxy_host }}:{{ profile.local_proxy_port }}</td>
            <td class="monospace">{{ profile.remote_bind_host }}:{{ profile.remote_port }}</td>
            <td><span class="soft-badge badge-success"><span class="badge-dot"></span>{{ formatBoolean(profile.auto_reconnect, 'enabled') }}</span></td>
            <td><span class="soft-badge badge-neutral">{{ formatProfileType(profile.profile_type) }}</span></td>
            <td>
              <div class="row-actions">
                <button type="button" :disabled="actionState[profile.name] && actionState[profile.name] !== 'idle'" :data-test="`connect-${profile.name}`" @click="$emit('connect', profile.name)">
                  {{ actionState[profile.name] === 'connecting' ? '连接中…' : '连接' }}
                </button>
                <button type="button" :disabled="actionState[profile.name] && actionState[profile.name] !== 'idle'" :data-test="`disconnect-${profile.name}`" @click="$emit('disconnect', profile.name)">
                  {{ actionState[profile.name] === 'disconnecting' ? '断开中…' : '断开' }}
                </button>
                <button type="button" :disabled="actionState[profile.name] && actionState[profile.name] !== 'idle'" :data-test="`doctor-${profile.name}`" @click="$emit('doctor', profile.name)">
                  {{ actionState[profile.name] === 'diagnosing' ? '诊断中…' : '诊断' }}
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="actionErrors[profile.name]" class="action-error-row">
            <td colspan="7">
              <strong>{{ actionErrors[profile.name].code }}</strong>
              <span>{{ actionErrors[profile.name].message }}</span>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
</template>
