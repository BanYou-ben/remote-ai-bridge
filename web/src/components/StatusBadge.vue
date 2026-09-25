<script setup>
import { computed } from 'vue'
import { formatSupervisorState } from '../utils/display.js'

const props = defineProps({
  status: { type: String, required: true },
})

const statusClass = computed(() => {
  if (props.status === 'READY') return 'healthy'
  if (['STARTING', 'CONNECTING', 'STOPPING'].includes(props.status)) return 'transitional'
  if (['DEGRADED', 'UNSUPERVISED'].includes(props.status)) return 'warning'
  if (props.status === 'FAILED') return 'error'
  return 'inactive'
})
</script>

<template>
  <span class="status-badge" :class="`status-${statusClass}`">{{ formatSupervisorState(status) }}</span>
</template>
