import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DashboardView from '../src/views/DashboardView.vue'
import {
  connectRuntime,
  disconnectRuntime,
  getHealth,
  getRuntime,
  listProfiles,
  listRuntime,
  runDoctor,
} from '../src/api/rab.js'

vi.mock('../src/api/rab.js', () => ({
  getHealth: vi.fn(),
  listProfiles: vi.fn(),
  listRuntime: vi.fn(),
  getRuntime: vi.fn(),
  connectRuntime: vi.fn(),
  disconnectRuntime: vi.fn(),
  runDoctor: vi.fn(),
}))

const profile = {
  name: 'lab-server',
  ssh_target: 'alice@example.test',
  local_proxy_host: '127.0.0.1',
  local_proxy_port: 7897,
  remote_bind_host: '127.0.0.1',
  remote_port: 17890,
  auto_reconnect: true,
  profile_type: 'managed',
}

function snapshot(state, extra = {}) {
  return {
    profile_name: 'lab-server',
    state,
    supervised: state !== 'STOPPED',
    process_alive: state === 'READY',
    remote_port: 17890,
    message: '',
    error_code: null,
    retry_in_seconds: null,
    ...extra,
  }
}

function doctorReport() {
  const result = (name, status) => ({ name, status, detail: `${name} detail`, error_code: null, http_status: null })
  return {
    local: {
      tcp: result('TCP', 'PASS'),
      handshake: result('Handshake', 'PASS'),
      endpoint: result('Endpoint', 'PASS'),
    },
    ssh: result('SSH', 'PASS'),
    tunnel: result('Tunnel', 'FAIL'),
    remote_listener: result('Listener', 'SKIP'),
    remote_endpoint: result('Remote endpoint', 'SKIP'),
  }
}

async function mountDashboard() {
  const wrapper = mount(DashboardView)
  await flushPromises()
  return wrapper
}

describe('runtime controls', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    getHealth.mockResolvedValue({ status: 'ok', service: 'remote-ai-bridge' })
    listProfiles.mockResolvedValue([profile])
    listRuntime.mockResolvedValue([snapshot('UNSUPERVISED')])
    connectRuntime.mockResolvedValue(snapshot('READY'))
    disconnectRuntime.mockResolvedValue(snapshot('STOPPED'))
    getRuntime.mockResolvedValue(snapshot('READY'))
    runDoctor.mockResolvedValue(doctorReport())
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('connects and disconnects from profile action buttons', async () => {
    const wrapper = await mountDashboard()
    await wrapper.get('[data-test="connect-lab-server"]').trigger('click')
    await flushPromises()
    expect(connectRuntime).toHaveBeenCalledWith('lab-server')
    await wrapper.get('[data-test="disconnect-lab-server"]').trigger('click')
    await flushPromises()
    expect(disconnectRuntime).toHaveBeenCalledWith('lab-server')
    expect(wrapper.text()).toContain('已停止')
  })

  it('polls STARTING and CONNECTING until READY', async () => {
    connectRuntime.mockResolvedValue(snapshot('STARTING'))
    getRuntime.mockResolvedValueOnce(snapshot('CONNECTING')).mockResolvedValueOnce(snapshot('READY'))
    const wrapper = await mountDashboard()
    await wrapper.get('[data-test="connect-lab-server"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(getRuntime).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('已就绪')
  })

  it('stops polling when runtime reaches FAILED', async () => {
    connectRuntime.mockResolvedValue(snapshot('STARTING'))
    getRuntime.mockResolvedValue(snapshot('FAILED', { error_code: 'SSH_FAILED', message: 'failure reason' }))
    const wrapper = await mountDashboard()
    await wrapper.get('[data-test="connect-lab-server"]').trigger('click')
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(getRuntime).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('SSH_FAILED')
    expect(wrapper.text()).toContain('failure reason')
  })

  it('reports a bounded polling timeout', async () => {
    connectRuntime.mockResolvedValue(snapshot('STARTING'))
    getRuntime.mockResolvedValue(snapshot('CONNECTING'))
    const wrapper = await mountDashboard()
    await wrapper.get('[data-test="connect-lab-server"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(30000)
    await flushPromises()
    expect(wrapper.text()).toContain('RUNTIME_POLL_TIMEOUT')
    expect(wrapper.text()).toContain('等待运行状态超时')
  })

  it('prevents duplicate actions for the same profile', async () => {
    let resolveConnect
    connectRuntime.mockReturnValue(new Promise((resolve) => { resolveConnect = resolve }))
    const wrapper = await mountDashboard()
    const button = wrapper.get('[data-test="connect-lab-server"]')
    await button.trigger('click')
    await button.trigger('click')
    expect(connectRuntime).toHaveBeenCalledTimes(1)
    expect(button.attributes('disabled')).toBeDefined()
    resolveConnect(snapshot('READY'))
    await flushPromises()
  })

  it('shows structured backend action errors', async () => {
    connectRuntime.mockRejectedValue({ code: 'PROFILE_BUSY', message: 'profile is busy', retryable: true, details: {} })
    const wrapper = await mountDashboard()
    await wrapper.get('[data-test="connect-lab-server"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('PROFILE_BUSY')
    expect(wrapper.text()).toContain('profile is busy')
  })

  it('runs doctor and renders the report', async () => {
    const wrapper = await mountDashboard()
    await wrapper.get('[data-test="doctor-lab-server"]').trigger('click')
    await flushPromises()
    expect(runDoctor).toHaveBeenCalledWith('lab-server')
    expect(wrapper.find('[data-test="doctor-panel"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('通过')
    expect(wrapper.text()).toContain('失败')
    expect(wrapper.text()).toContain('跳过')
  })

  it('runs doctor while the runtime is READY', async () => {
    listRuntime.mockResolvedValue([snapshot('READY')])
    const wrapper = await mountDashboard()
    await wrapper.get('[data-test="doctor-lab-server"]').trigger('click')
    await flushPromises()
    expect(runDoctor).toHaveBeenCalledWith('lab-server')
    expect(wrapper.text()).toContain('连接诊断')
  })

  it('shows doctor API failures without blanking the dashboard', async () => {
    runDoctor.mockRejectedValue({ code: 'BACKEND_UNAVAILABLE', message: 'Backend unavailable.' })
    const wrapper = await mountDashboard()
    await wrapper.get('[data-test="doctor-lab-server"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('诊断失败')
    expect(wrapper.text()).toContain('BACKEND_UNAVAILABLE')
    expect(wrapper.text()).toContain('连接配置')
  })

  it('cleans up polling when the component unmounts', async () => {
    connectRuntime.mockResolvedValue(snapshot('STARTING'))
    const wrapper = await mountDashboard()
    await wrapper.get('[data-test="connect-lab-server"]').trigger('click')
    await flushPromises()
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(5000)
    expect(getRuntime).not.toHaveBeenCalled()
  })
})
