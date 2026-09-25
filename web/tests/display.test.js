import { describe, expect, it } from 'vitest'

import {
  formatBoolean,
  formatProfileType,
  formatRuntimeMessage,
  formatSupervisorState,
} from '../src/utils/display.js'

describe('display formatters', () => {
  it.each([
    ['READY', '已就绪'],
    ['STARTING', '正在启动'],
    ['CONNECTING', '正在连接'],
    ['DEGRADED', '状态异常'],
    ['FAILED', '失败'],
    ['STOPPING', '正在停止'],
    ['STOPPED', '已停止'],
    ['UNSUPERVISED', '未托管'],
  ])('maps supervisor state %s', (state, label) => {
    expect(formatSupervisorState(state)).toBe(label)
  })

  it('formats boolean and profile type values', () => {
    expect(formatBoolean(true, 'enabled')).toBe('已开启')
    expect(formatBoolean(false, 'enabled')).toBe('已关闭')
    expect(formatBoolean(true)).toBe('是')
    expect(formatBoolean(false)).toBe('否')
    expect(formatBoolean(null)).toBe('未知')
    expect(formatProfileType('legacy')).toBe('旧版')
    expect(formatProfileType('managed')).toBe('托管')
  })

  it.each([
    ['runtime evidence exists but the process identity is absent or mismatched', '存在运行记录，但对应进程不存在或身份不匹配'],
    ['profile is not supervised and has no runtime evidence', '当前配置未被托管，且没有运行记录'],
    ['bridge is healthy', '连接运行正常'],
    ['supervision stopped', '连接已停止'],
  ])('maps known runtime message exactly', (message, label) => {
    expect(formatRuntimeMessage(message)).toBe(label)
  })

  it('returns unknown runtime messages unchanged', () => {
    const message = 'a new backend protocol message'
    expect(formatRuntimeMessage(message)).toBe(message)
  })
})
