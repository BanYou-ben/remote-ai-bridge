import { onUnmounted, reactive } from 'vue'

import { connectRuntime, disconnectRuntime, getRuntime, runDoctor } from '../api/rab.js'

const CONNECT_TERMINAL_STATES = new Set(['READY', 'FAILED', 'DEGRADED'])
const DISCONNECT_TERMINAL_STATES = new Set(['STOPPED', 'FAILED'])

export function useRuntimeControls({ onRuntimeUpdate, pollIntervalMs = 1000, timeoutMs = 30000 } = {}) {
  const actionState = reactive({})
  const actionErrors = reactive({})
  const doctor = reactive({ open: false, profileName: '', loading: false, report: null, error: null })
  const waits = new Map()
  let disposed = false

  function stateFor(name) {
    return actionState[name] ?? 'idle'
  }

  function isBusy(name) {
    return stateFor(name) !== 'idle'
  }

  function updateRuntime(snapshot) {
    if (!disposed && snapshot) onRuntimeUpdate?.(snapshot)
  }

  function waitForNextPoll() {
    if (disposed) return Promise.resolve(false)
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        waits.delete(timer)
        resolve(!disposed)
      }, pollIntervalMs)
      waits.set(timer, resolve)
    })
  }

  async function pollRuntime(name, terminalStates) {
    const startedAt = Date.now()
    while (!disposed && Date.now() - startedAt < timeoutMs) {
      if (!(await waitForNextPoll())) return null
      const snapshot = await getRuntime(name)
      updateRuntime(snapshot)
      if (terminalStates.has(snapshot.state)) return snapshot
    }
    if (disposed) return null
    throw {
      code: 'RUNTIME_POLL_TIMEOUT',
      message: '等待运行状态超时，请稍后刷新确认。',
      retryable: true,
      details: { name },
    }
  }

  async function performRuntimeAction(name, action, request, terminalStates) {
    if (isBusy(name) || disposed) return
    actionState[name] = action
    actionErrors[name] = null
    try {
      const initial = await request(name)
      updateRuntime(initial)
      if (!terminalStates.has(initial.state)) await pollRuntime(name, terminalStates)
    } catch (error) {
      if (!disposed) actionErrors[name] = error
    } finally {
      if (!disposed) actionState[name] = 'idle'
    }
  }

  function connect(name) {
    return performRuntimeAction(name, 'connecting', connectRuntime, CONNECT_TERMINAL_STATES)
  }

  function disconnect(name) {
    return performRuntimeAction(name, 'disconnecting', disconnectRuntime, DISCONNECT_TERMINAL_STATES)
  }

  async function diagnose(name) {
    if (isBusy(name) || disposed) return
    actionState[name] = 'diagnosing'
    actionErrors[name] = null
    doctor.open = true
    doctor.profileName = name
    doctor.loading = true
    doctor.report = null
    doctor.error = null
    try {
      const report = await runDoctor(name)
      if (!disposed) doctor.report = report
    } catch (error) {
      if (!disposed) doctor.error = error
    } finally {
      if (!disposed) {
        doctor.loading = false
        actionState[name] = 'idle'
      }
    }
  }

  function closeDoctor() {
    doctor.open = false
  }

  function dispose() {
    disposed = true
    for (const [timer, resolve] of waits) {
      clearTimeout(timer)
      resolve(false)
    }
    waits.clear()
  }

  onUnmounted(dispose)

  return {
    actionState,
    actionErrors,
    doctor,
    stateFor,
    isBusy,
    connect,
    disconnect,
    diagnose,
    closeDoctor,
    dispose,
  }
}
