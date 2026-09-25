import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

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

const runtime = {
  profile_name: 'lab-server',
  state: 'READY',
  supervised: true,
  process_alive: true,
  remote_port: 17890,
  message: 'bridge is healthy',
}

function successfulRequests({ profiles = [profile], runtimeItems = [runtime] } = {}) {
  getHealth.mockResolvedValue({ status: 'ok', service: 'remote-ai-bridge' })
  listProfiles.mockResolvedValue(profiles)
  listRuntime.mockResolvedValue(runtimeItems)
}

describe('DashboardView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    successfulRequests()
    connectRuntime.mockResolvedValue({ ...runtime, state: 'READY' })
    disconnectRuntime.mockResolvedValue({ ...runtime, state: 'STOPPED' })
    getRuntime.mockResolvedValue(runtime)
    runDoctor.mockResolvedValue({})
  })

  it('shows independent loading states', () => {
    getHealth.mockReturnValue(new Promise(() => {}))
    listProfiles.mockReturnValue(new Promise(() => {}))
    listRuntime.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(DashboardView)
    expect(wrapper.text()).toContain('正在检查后端服务')
    expect(wrapper.text()).toContain('正在加载连接配置')
    expect(wrapper.text()).toContain('正在加载运行状态')
  })

  it('shows backend health success', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('后端服务')
    expect(wrapper.text()).toContain('在线')
    expect(wrapper.text()).toContain('本地服务运行正常')
  })

  it('shows backend unavailable without hiding other sections', async () => {
    getHealth.mockRejectedValue({ code: 'BACKEND_UNAVAILABLE', message: 'Backend unavailable.' })
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('后端服务不可用')
    expect(wrapper.text()).toContain('离线')
    expect(wrapper.text()).toContain('lab-server')
  })

  it('shows the profiles empty state', async () => {
    successfulRequests({ profiles: [] })
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('暂无连接配置')
  })

  it('renders profile and runtime data', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.text()).toContain('alice@example.test')
    expect(wrapper.text()).toContain('127.0.0.1:7897')
    expect(wrapper.text()).toContain('已就绪')
    expect(wrapper.text()).toContain('连接运行正常')
  })

  it('refreshes all read-only resources on demand', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(getHealth).toHaveBeenCalledTimes(2)
    expect(listProfiles).toHaveBeenCalledTimes(2)
    expect(listRuntime).toHaveBeenCalledTimes(2)
  })

  it('uses Chinese dashboard headings and boolean labels', async () => {
    const wrapper = mount(DashboardView)
    await flushPromises()
    expect(wrapper.get('h1').text()).toBe('总览')
    expect(wrapper.text()).toContain('连接配置')
    expect(wrapper.text()).toContain('运行状态')
    expect(wrapper.text()).toContain('已开启')
    expect(wrapper.text()).toContain('是')
  })
})
