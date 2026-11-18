<script setup>
import { ref } from 'vue'
import { runAIDiagnosis } from '../api/rab.js'

const props = defineProps({ profiles: { type: Array, required: true } })
const selected = ref('')
const question = ref('请检查连接与服务器资源，并基于证据诊断。')
const key = ref('')
const consent = ref(false)
const pending = ref(false)
const result = ref(null)
const error = ref(null)

async function run() {
  if (pending.value || !selected.value || !question.value.trim() || !key.value.trim() || !consent.value) return
  pending.value = true
  result.value = null
  error.value = null
  const credential = key.value
  key.value = ''
  try {
    result.value = await runAIDiagnosis(selected.value, { question: question.value, api_key: credential, consent: true })
  } catch (failure) {
    error.value = { code: failure?.code || 'AI_UNAVAILABLE' }
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <section class="section-card dashboard-section ai-workbench" aria-labelledby="ai-title">
    <div class="section-heading"><div><p class="panel-kicker">AI WORKBENCH</p><h2 id="ai-title">AI 故障诊断</h2><p>基于受控工具和真实证据，辅助分析连接问题。</p></div><span class="ai-provider">DeepSeek · 只读诊断</span></div>
    <p class="ai-notice">问题、工具名称、匿名运行状态、部分资源数值及证据编号和状态会发送给 DeepSeek。请勿在问题中输入密码、密钥或个人信息。可能产生 API 费用。</p>
    <div class="ai-layout"><form class="ai-form" @submit.prevent="run">
      <p><label>连接配置 <select v-model="selected" data-test="ai-profile" required><option value="" disabled>选择连接</option><option v-for="profile in props.profiles" :key="profile.name" :value="profile.name">{{ profile.name }}</option></select></label></p>
      <p><label>诊断问题 <textarea v-model="question" data-test="ai-question" maxlength="500" required /></label></p>
      <p><label>DeepSeek API Key <input v-model="key" data-test="ai-key" type="password" autocomplete="off" required /></label></p>
      <p><label><input v-model="consent" data-test="ai-consent" type="checkbox" required /> 我同意将上述信息发送给 DeepSeek，并了解可能产生费用。</label></p>
      <button class="primary-button" data-test="ai-submit" type="submit" :disabled="pending || !consent || !selected">{{ pending ? '诊断中，请稍候…' : '开始 AI 诊断' }}</button>
    </form>
    <div class="ai-output" aria-live="polite">
    <div class="ai-output-heading"><span class="ai-output-icon" aria-hidden="true">✧</span><div><strong>诊断工作区</strong><span>工具记录与证据将在此显示</span></div></div>
    <p v-if="pending" role="status">正在运行工具和模型；完成后显示执行记录。</p>
    <p v-if="error" role="alert">AI 诊断不可用（{{ error.code }}）。仍可使用原有“运行诊断”。</p>
    <div v-if="result" data-test="ai-result">
      <p>诊断状态：{{ result.status === 'completed' ? '完成' : '失败' }} <span v-if="result.error_code">{{ result.error_code }}</span></p>
      <p v-if="result.status !== 'completed'">可使用原有“运行诊断”继续排查。</p>
      <h3>工具执行记录</h3><ul><li v-for="(tool, index) in result.tools" :key="index">{{ tool.name }}：{{ tool.ok ? '执行成功' : '执行失败' }}</li></ul>
      <template v-if="result.diagnosis"><h3>诊断结论</h3><p>{{ result.diagnosis.summary }}</p><p>类别：{{ result.diagnosis.stage }}</p></template>
      <h3>证据</h3><ul><li v-for="item in result.evidence" :key="item.id">{{ item.id }}：{{ item.status }}</li></ul>
    </div>
    <div v-else-if="!pending && !error" class="ai-output-empty">选择连接并描述问题，开始一次诊断。</div>
    </div></div>
  </section>
</template>
