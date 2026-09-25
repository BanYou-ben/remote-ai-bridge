import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ProfileDetailView from '../src/views/ProfileDetailView.vue'
import { deleteProfile, getProfile, updateProfile } from '../src/api/rab.js'

const push = vi.fn()
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))
vi.mock('../src/api/rab.js', () => ({
  getProfile: vi.fn(),
  updateProfile: vi.fn(),
  deleteProfile: vi.fn(),
}))

const managedProfile = {
  schema_version: 2,
  name: 'managed-lab',
  profile_type: 'managed',
  host: 'example.test',
  username: 'alice',
  ssh_port: 22,
  ssh_target: 'example.test',
  local_proxy_host: '127.0.0.1',
  local_proxy_port: 7897,
  remote_bind_host: '127.0.0.1',
  remote_port: 17890,
  auto_reconnect: true,
  endpoint_probe_url: 'https://api.openai.com/v1/models',
  host_key_type: 'ssh-ed25519',
  host_key_fingerprint: `SHA256:${'A'.repeat(43)}`,
  key_id: 'PRIVATE-METADATA-MUST-NOT-RENDER',
}

function mountView(profile = managedProfile) {
  getProfile.mockResolvedValueOnce(profile)
  return mount(ProfileDetailView, {
    props: { name: profile.name },
    global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } },
  })
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('ProfileDetailView', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders complete safe profile metadata without credential material', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('managed-lab')
    expect(wrapper.text()).toContain('example.test')
    expect(wrapper.text()).toContain('alice')
    expect(wrapper.text()).toContain('ssh-ed25519')
    expect(wrapper.text()).toContain('SHA256:')
    expect(wrapper.text()).toContain('由 Remote AI Bridge 管理 SSH 凭据')
    expect(wrapper.text()).not.toContain(managedProfile.key_id)
  })

  it('updates only the four fields exposed by the safe form', async () => {
    const wrapper = mountView()
    await flushPromises()
    const numbers = wrapper.findAll('input[type="number"]')
    await numbers[0].setValue('7890')
    await numbers[1].setValue('17891')
    updateProfile.mockResolvedValueOnce({ ...managedProfile, local_proxy_port: 7890, remote_port: 17891 })
    await wrapper.get('.profile-edit-form').trigger('submit')
    await flushPromises()
    expect(updateProfile).toHaveBeenCalledWith('managed-lab', {
      local_proxy_port: 7890,
      remote_port: 17891,
      auto_reconnect: true,
      endpoint_probe_url: 'https://api.openai.com/v1/models',
    })
  })

  it('shows dirty state when an editable field changes', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('[data-test="save-status"]').exists()).toBe(false)
    await wrapper.get('[data-test="remote-port"]').setValue('17891')
    expect(wrapper.get('[data-test="save-status"]').text()).toBe('有未保存的修改')
  })

  it('shows saving state and disables duplicate submission while PATCH is pending', async () => {
    const pending = deferred()
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-test="remote-port"]').setValue('17891')
    updateProfile.mockReturnValueOnce(pending.promise)
    await wrapper.get('.profile-edit-form').trigger('submit')
    expect(wrapper.get('[data-test="save-profile"]').text()).toBe('正在保存…')
    expect(wrapper.get('[data-test="save-profile"]').attributes('disabled')).toBeDefined()
    await wrapper.get('.profile-edit-form').trigger('submit')
    expect(updateProfile).toHaveBeenCalledTimes(1)
    pending.resolve({ ...managedProfile, remote_port: 17891 })
    await flushPromises()
  })

  it('keeps saved feedback visible until another edit makes the form dirty', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-test="remote-port"]').setValue('17891')
    updateProfile.mockResolvedValueOnce({ ...managedProfile, remote_port: 17891 })
    await wrapper.get('.profile-edit-form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[data-test="save-status"]').text()).toBe('✓ 已保存')
    await flushPromises()
    expect(wrapper.get('[data-test="save-status"]').text()).toBe('✓ 已保存')
    await wrapper.get('[data-test="local-proxy-port"]').setValue('7890')
    expect(wrapper.get('[data-test="save-status"]').text()).toBe('有未保存的修改')
  })

  it('shows save failure and structured error without false success', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-test="remote-port"]').setValue('17891')
    updateProfile.mockRejectedValueOnce({ code: 'PROFILE_BUSY', message: 'profile is in use', details: {} })
    await wrapper.get('.profile-edit-form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[data-test="save-status"]').text()).toBe('保存失败')
    expect(wrapper.text()).toContain('PROFILE_BUSY')
    expect(wrapper.text()).not.toContain('✓ 已保存')
    expect(wrapper.get('[data-test="save-profile"]').attributes('disabled')).toBeUndefined()
  })

  it('persists auto reconnect true to false through the actual save path', async () => {
    const wrapper = mountView({ ...managedProfile, auto_reconnect: true })
    await flushPromises()
    await wrapper.get('[data-test="auto-reconnect"]').setValue(false)
    expect(wrapper.get('[data-test="save-status"]').text()).toBe('有未保存的修改')
    updateProfile.mockResolvedValueOnce({ ...managedProfile, auto_reconnect: false })
    await wrapper.get('.profile-edit-form').trigger('submit')
    await flushPromises()
    expect(updateProfile.mock.calls[0][1].auto_reconnect).toBe(false)
    expect(wrapper.get('[data-test="auto-reconnect"]').element.checked).toBe(false)
    expect(wrapper.get('[data-test="save-status"]').text()).toBe('✓ 已保存')
  })

  it('persists auto reconnect false to true through the actual save path', async () => {
    const initial = { ...managedProfile, auto_reconnect: false }
    const wrapper = mountView(initial)
    await flushPromises()
    await wrapper.get('[data-test="auto-reconnect"]').setValue(true)
    expect(wrapper.get('[data-test="save-status"]').text()).toBe('有未保存的修改')
    updateProfile.mockResolvedValueOnce({ ...initial, auto_reconnect: true })
    await wrapper.get('.profile-edit-form').trigger('submit')
    await flushPromises()
    expect(updateProfile.mock.calls[0][1].auto_reconnect).toBe(true)
    expect(wrapper.get('[data-test="auto-reconnect"]').element.checked).toBe(true)
    expect(wrapper.get('[data-test="save-status"]').text()).toBe('✓ 已保存')
  })

  it('shows PROFILE_BUSY without bypassing the backend', async () => {
    const wrapper = mountView()
    await flushPromises()
    updateProfile.mockRejectedValueOnce({ code: 'PROFILE_BUSY', message: 'profile is in use', details: {} })
    await wrapper.get('.profile-edit-form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('PROFILE_BUSY')
    expect(wrapper.text()).toContain('profile is in use')
  })

  it('requires a confirmation containing the exact profile name before legacy delete', async () => {
    const legacy = { ...managedProfile, name: 'legacy-a', profile_type: 'legacy' }
    const wrapper = mountView(legacy)
    await flushPromises()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    deleteProfile.mockResolvedValueOnce({ name: 'legacy-a' })
    await wrapper.get('.danger-button').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledWith('确定删除连接配置 legacy-a 吗？')
    expect(deleteProfile).toHaveBeenCalledWith('legacy-a')
    expect(push).toHaveBeenCalledWith('/profiles')
  })

  it('does not delete when confirmation is declined', async () => {
    const wrapper = mountView()
    await flushPromises()
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    await wrapper.get('.danger-button').trigger('click')
    expect(deleteProfile).not.toHaveBeenCalled()
  })

  it('shows the managed credential cleanup boundary on backend 409', async () => {
    const wrapper = mountView()
    await flushPromises()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    deleteProfile.mockRejectedValueOnce({
      code: 'MANAGED_PROFILE_CREDENTIAL_CLEANUP_REQUIRED',
      message: 'credential cleanup required',
      details: {},
    })
    await wrapper.get('.danger-button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('MANAGED_PROFILE_CREDENTIAL_CLEANUP_REQUIRED')
    expect(wrapper.text()).toContain('当前网页暂不支持直接删除')
    expect(push).not.toHaveBeenCalled()
  })
})
