import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ProfileTable from '../src/components/ProfileTable.vue'

describe('ProfileTable management entry', () => {
  it('links management to the encoded profile detail route', () => {
    const profile = {
      name: 'team/profile',
      ssh_target: 'alice@example.test',
      local_proxy_host: '127.0.0.1',
      local_proxy_port: 7897,
      remote_bind_host: '127.0.0.1',
      remote_port: 17890,
      auto_reconnect: true,
      profile_type: 'managed',
    }
    const wrapper = mount(ProfileTable, {
      props: { profiles: [profile] },
      global: {
        stubs: {
          RouterLink: {
            props: ['to'],
            template: '<a :href="to"><slot /></a>',
          },
        },
      },
    })

    const manage = wrapper.get('[data-test="manage-team/profile"]')
    expect(manage.text()).toBe('管理')
    expect(manage.attributes('href')).toBe('/profiles/team%2Fprofile')
    expect(wrapper.text()).not.toContain('删除')
  })
})
