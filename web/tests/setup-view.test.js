import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import SetupView from '../src/views/SetupView.vue'
import {
  confirmHost,
  discoverLocalProxy,
  prepareHost,
  setupManagedProfile,
} from '../src/api/rab.js'

vi.mock('../src/api/rab.js', () => ({
  prepareHost: vi.fn(),
  confirmHost: vi.fn(),
  discoverLocalProxy: vi.fn(),
  setupManagedProfile: vi.fn(),
}))

const fingerprint = `SHA256:${'A'.repeat(43)}`
const hostPreparation = {
  host: 'example.test',
  port: 22,
  key_type: 'ssh-ed25519',
  fingerprint,
  status: 'READY_TO_AUTH',
}
const healthyProxy = {
  host: '127.0.0.1',
  port: 7897,
  tcp_reachable: true,
  connect_reachable: true,
  endpoint_reachable: true,
  error_code: null,
}
const failedProxy = {
  host: '127.0.0.1',
  port: 7890,
  tcp_reachable: true,
  connect_reachable: false,
  endpoint_reachable: false,
  error_code: 'PROXY_HANDSHAKE_FAILED',
}
const managedProfile = {
  name: 'managed-lab',
  profile_type: 'managed',
  host: 'example.test',
  username: 'alice',
  ssh_port: 22,
  local_proxy_host: '127.0.0.1',
  local_proxy_port: 7897,
  remote_bind_host: '127.0.0.1',
  remote_port: 17890,
  auto_reconnect: true,
}

function mountView() {
  return mount(SetupView, {
    global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } },
  })
}

async function fillServer(wrapper) {
  await wrapper.get('[data-test="name"]').setValue('managed-lab')
  await wrapper.get('[data-test="host"]').setValue('example.test')
  await wrapper.get('[data-test="username"]').setValue('alice')
  await wrapper.get('[data-test="ssh-port"]').setValue('22')
}

async function prepareUnknownHost(wrapper) {
  prepareHost.mockRejectedValueOnce({
    code: 'HOST_KEY_CONFIRMATION_REQUIRED',
    message: 'confirmation required',
    retryable: false,
    details: { ...hostPreparation },
  })
  await fillServer(wrapper)
  await wrapper.get('form').trigger('submit')
  await flushPromises()
}

async function reachPasswordStep(wrapper) {
  await prepareUnknownHost(wrapper)
  confirmHost.mockResolvedValueOnce(hostPreparation)
  await wrapper.get('[data-test="confirm-fingerprint"]').trigger('click')
  await flushPromises()
  discoverLocalProxy.mockResolvedValueOnce({ selected: healthyProxy, candidates: [healthyProxy] })
  await wrapper.get('[data-test="discover-proxy"]').trigger('click')
  await flushPromises()
  await wrapper.get('[data-test="continue-setup"]').trigger('click')
}

describe('SetupView managed setup wizard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    sessionStorage.clear()
  })

  it('renders the server information first step', () => {
    const wrapper = mountView()
    expect(wrapper.get('h1').text()).toBe('添加托管连接')
    expect(wrapper.text()).toContain('服务器信息')
    expect(wrapper.find('[data-test="password"]').exists()).toBe(false)
  })

  it('prepares a known host without sending a password', async () => {
    prepareHost.mockResolvedValueOnce(hostPreparation)
    const wrapper = mountView()
    await fillServer(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(prepareHost).toHaveBeenCalledWith({ host: 'example.test', username: 'alice', port: 22 })
    expect(wrapper.text()).toContain(fingerprint)
    expect(confirmHost).not.toHaveBeenCalled()
  })

  it('shows a required fingerprint and only confirms on an explicit click', async () => {
    const wrapper = mountView()
    await prepareUnknownHost(wrapper)
    expect(wrapper.text()).toContain(fingerprint)
    expect(wrapper.text()).toContain('该主机尚未受信任')
    expect(confirmHost).not.toHaveBeenCalled()

    confirmHost.mockResolvedValueOnce(hostPreparation)
    await wrapper.get('[data-test="confirm-fingerprint"]').trigger('click')
    await flushPromises()
    expect(confirmHost).toHaveBeenCalledWith({
      host: 'example.test',
      port: 22,
      expected_fingerprint: fingerprint,
      accepted: true,
    })
  })

  it('invalidates fingerprint confirmation after the host changes', async () => {
    const wrapper = mountView()
    await prepareUnknownHost(wrapper)
    confirmHost.mockResolvedValueOnce(hostPreparation)
    await wrapper.get('[data-test="confirm-fingerprint"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="edit-server"]').trigger('click')
    await wrapper.get('[data-test="host"]').setValue('changed.example.test')
    expect(wrapper.text()).toContain('服务器信息')
    expect(wrapper.find('[data-test="discover-proxy"]').exists()).toBe(false)
  })

  it('shows a host key change without accepting the replacement fingerprint', async () => {
    const wrapper = mountView()
    await prepareUnknownHost(wrapper)
    confirmHost.mockRejectedValueOnce({ code: 'HOST_KEY_CHANGED', message: 'host key changed', details: {} })
    await wrapper.get('[data-test="confirm-fingerprint"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('HOST_KEY_CHANGED')
    expect(wrapper.find('[data-test="discover-proxy"]').exists()).toBe(false)
  })

  it('renders proxy candidates and prevents selecting a failed candidate', async () => {
    prepareHost.mockResolvedValueOnce(hostPreparation)
    const wrapper = mountView()
    await fillServer(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-test="accept-known-host"]').trigger('click')
    discoverLocalProxy.mockResolvedValueOnce({ selected: healthyProxy, candidates: [failedProxy, healthyProxy] })
    await wrapper.get('[data-test="discover-proxy"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('127.0.0.1:7890')
    expect(wrapper.get('[data-test="proxy-7890"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-test="proxy-7897"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('已选择本地代理：127.0.0.1:7897')
  })

  it('shows proxy discovery failure candidates without selecting them', async () => {
    prepareHost.mockResolvedValueOnce(hostPreparation)
    const wrapper = mountView()
    await fillServer(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-test="accept-known-host"]').trigger('click')
    discoverLocalProxy.mockRejectedValueOnce({
      code: 'LOCAL_PROXY_NOT_FOUND',
      message: 'no healthy proxy found',
      details: { candidates: [failedProxy] },
    })
    await wrapper.get('[data-test="discover-proxy"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('LOCAL_PROXY_NOT_FOUND')
    expect(wrapper.get('[data-test="proxy-7890"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-test="continue-setup"]').attributes('disabled')).toBeDefined()
  })

  it('creates a managed profile and clears the password after success', async () => {
    const wrapper = mountView()
    await reachPasswordStep(wrapper)
    const secret = 'RAB-P43-SECRET'
    await wrapper.get('[data-test="password"]').setValue(secret)
    setupManagedProfile.mockResolvedValueOnce(managedProfile)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(setupManagedProfile.mock.calls[0][0]).toMatchObject({
      name: 'managed-lab',
      confirmed_fingerprint: fingerprint,
      selected_local_proxy_port: 7897,
      password: secret,
    })
    expect(wrapper.text()).toContain('连接配置已创建')
    expect(wrapper.text()).not.toContain(secret)
  })

  it.each([
    ['PROFILE_EXISTS', 'profile already exists'],
    ['SSH_BOOTSTRAP_FAILED', 'password authentication failed'],
    ['REMOTE_PORT_RANGE_EXHAUSTED', 'remote port unavailable'],
    ['REQUEST_INVALID', 'request validation failed'],
    ['BACKEND_UNAVAILABLE', 'Backend unavailable.'],
  ])('shows structured %s errors and clears password after failure', async (code, message) => {
    const wrapper = mountView()
    await reachPasswordStep(wrapper)
    const secret = `secret-${code}`
    await wrapper.get('[data-test="password"]').setValue(secret)
    setupManagedProfile.mockRejectedValueOnce({ code, message, retryable: false, details: {} })
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain(code)
    expect(wrapper.text()).toContain(message)
    expect(wrapper.get('[data-test="password"]').element.value).toBe('')
    expect(wrapper.text()).not.toContain(secret)
  })

  it('never persists or logs the password', async () => {
    const localWrite = vi.spyOn(Storage.prototype, 'setItem')
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    const wrapper = mountView()
    await reachPasswordStep(wrapper)
    const secret = 'NEVER-PERSIST-P43'
    await wrapper.get('[data-test="password"]').setValue(secret)
    setupManagedProfile.mockRejectedValueOnce({ code: 'PROFILE_EXISTS', message: 'exists', details: {} })
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(localWrite).not.toHaveBeenCalled()
    expect(localStorage.getItem('password')).toBeNull()
    expect(sessionStorage.getItem('password')).toBeNull()
    expect(log).not.toHaveBeenCalled()
  })

  it('shows backend unavailable during host preparation', async () => {
    prepareHost.mockRejectedValueOnce({ code: 'BACKEND_UNAVAILABLE', message: 'Backend unavailable.', details: {} })
    const wrapper = mountView()
    await fillServer(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('BACKEND_UNAVAILABLE')
  })
})
