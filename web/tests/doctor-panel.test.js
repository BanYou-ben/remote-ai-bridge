import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import DoctorPanel from '../src/components/DoctorPanel.vue'

function check(name, status, detail, extra = {}) {
  return { name, status, detail, error_code: null, http_status: null, ...extra }
}

const report = {
  local: {
    tcp: check('TCP reachable', 'PASS', 'connection accepted'),
    handshake: check('HTTP proxy handshake', 'FAIL', 'proxy rejected', { error_code: 'PROXY_FAILED' }),
    endpoint: check('Endpoint probe', 'SKIP', 'proxy unavailable'),
  },
  ssh: check('SSH configuration', 'PASS', 'target resolves'),
  tunnel: check('Tunnel process', 'PASS', 'owned process is running'),
  remote_listener: check('Remote listener', 'PASS', 'listener is available'),
  remote_endpoint: check('Remote endpoint', 'PASS', 'endpoint returned', { http_status: 401 }),
}

describe('DoctorPanel', () => {
  it('renders PASS, FAIL and SKIP checks with complete details', () => {
    const wrapper = mount(DoctorPanel, {
      props: { state: { open: true, profileName: 'lab-server', loading: false, report, error: null } },
    })
    expect(wrapper.text()).toContain('通过')
    expect(wrapper.text()).toContain('失败')
    expect(wrapper.text()).toContain('跳过')
    expect(wrapper.text()).toContain('proxy rejected')
    expect(wrapper.text()).toContain('PROXY_FAILED')
    expect(wrapper.text()).toContain('401')
  })

  it('renders API failures and can retry or close', async () => {
    const wrapper = mount(DoctorPanel, {
      props: {
        state: {
          open: true,
          profileName: 'lab-server',
          loading: false,
          report: null,
          error: { code: 'BACKEND_UNAVAILABLE', message: 'Backend unavailable.' },
        },
      },
    })
    expect(wrapper.text()).toContain('诊断失败')
    expect(wrapper.text()).toContain('BACKEND_UNAVAILABLE')
    await wrapper.get('.primary-button').trigger('click')
    await wrapper.get('[aria-label="关闭诊断"]').trigger('click')
    expect(wrapper.emitted('retry')[0]).toEqual(['lab-server'])
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
