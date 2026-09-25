import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ProfilesView from '../src/views/ProfilesView.vue'
import { listProfiles } from '../src/api/rab.js'

vi.mock('../src/api/rab.js', () => ({ listProfiles: vi.fn() }))

describe('ProfilesView', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows the empty state', async () => {
    listProfiles.mockResolvedValue([])
    const wrapper = mount(ProfilesView)
    await flushPromises()
    expect(wrapper.text()).toContain('暂无连接配置')
  })

  it('renders managed metadata without key or password content', async () => {
    listProfiles.mockResolvedValue([
      {
        name: 'managed-lab',
        profile_type: 'managed',
        ssh_target: 'alice@example.test',
        host: 'example.test',
        username: 'alice',
        ssh_port: 22,
        local_proxy_host: '127.0.0.1',
        local_proxy_port: 7897,
        remote_bind_host: '127.0.0.1',
        remote_port: 17890,
        auto_reconnect: true,
        host_key_type: 'ssh-ed25519',
        host_key_fingerprint: 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        key_id: 'a'.repeat(32),
      },
    ])
    const wrapper = mount(ProfilesView)
    await flushPromises()
    expect(wrapper.text()).toContain('managed-lab')
    expect(wrapper.get('h1').text()).toBe('连接配置')
    expect(wrapper.text()).toContain('托管')
    expect(wrapper.text()).toContain('自动重连')
    expect(wrapper.text()).toContain('已开启')
    expect(wrapper.text()).toContain('example.test')
    expect(wrapper.text()).toContain('ssh-ed25519')
    expect(wrapper.text()).toContain('SHA256:')
    expect(wrapper.text()).not.toContain('a'.repeat(32))
    expect(wrapper.text().toLowerCase()).not.toContain('password')
  })
})
