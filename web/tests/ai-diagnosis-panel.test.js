import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import AIDiagnosisPanel from '../src/components/AIDiagnosisPanel.vue'
import { runAIDiagnosis } from '../src/api/rab.js'

vi.mock('../src/api/rab.js', () => ({ runAIDiagnosis: vi.fn() }))

describe('AI diagnosis', () => {
  it('requires consent, clears key immediately, prevents duplicate submission and renders evidence', async () => {
    let resolveRequest
    runAIDiagnosis.mockReturnValue(new Promise((resolve) => { resolveRequest = resolve }))
    const wrapper = mount(AIDiagnosisPanel, { props: { profiles: [{ name: 'server' }] } })
    await wrapper.get('[data-test="ai-profile"]').setValue('server')
    await wrapper.get('[data-test="ai-key"]').setValue('fixture-secret')
    expect(wrapper.get('[data-test="ai-submit"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-test="ai-consent"]').setValue(true)
    await wrapper.get('form').trigger('submit')
    await wrapper.get('form').trigger('submit')
    expect(runAIDiagnosis).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[data-test="ai-key"]').element.value).toBe('')
    expect(wrapper.text()).toContain('诊断中')
    resolveRequest({ status: 'completed', error_code: null, tools: [{ name: 'run_doctor', ok: true }], evidence: [{ id: 'connection.ssh.config', status: 'PASS' }], diagnosis: { stage: 'connection.ssh', summary: '检查完成', evidence_ids: ['connection.ssh.config'] } })
    await flushPromises()
    expect(wrapper.text()).toContain('检查完成')
    expect(wrapper.text()).toContain('connection.ssh.config')
    expect(wrapper.text()).not.toContain('fixture-secret')
  })

  it('shows fallback when AI is unavailable', async () => {
    runAIDiagnosis.mockReset().mockRejectedValue({ code: 'BACKEND_UNAVAILABLE' })
    const wrapper = mount(AIDiagnosisPanel, { props: { profiles: [{ name: 'server' }] } })
    await wrapper.get('[data-test="ai-profile"]').setValue('server')
    await wrapper.get('[data-test="ai-key"]').setValue('fixture-secret')
    await wrapper.get('[data-test="ai-consent"]').setValue(true)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('仍可使用原有“运行诊断”')
    expect(wrapper.text()).not.toContain('fixture-secret')
  })
})
