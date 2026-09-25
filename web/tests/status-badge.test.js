import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import StatusBadge from '../src/components/StatusBadge.vue'

describe('StatusBadge', () => {
  it.each([
    ['READY', '已就绪', 'status-healthy'],
    ['FAILED', '失败', 'status-error'],
    ['UNSUPERVISED', '未托管', 'status-warning'],
  ])('shows the Chinese label for %s with its semantic class', (status, label, className) => {
    const wrapper = mount(StatusBadge, { props: { status } })
    expect(wrapper.text()).toBe(label)
    expect(wrapper.classes()).toContain(className)
  })
})
