<script setup>
/** GSAP 数字滚动：KPI 从旧值平滑过渡到新值，尊重 reduced-motion。 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { gsap, reduceMotion } from '../motion'

const props = defineProps({
  value: { type: Number, default: 0 },
  duration: { type: Number, default: 0.9 },
  compact: { type: Boolean, default: false }
})

const display = ref('0')
const state = { value: 0 }
let tween = null

function format(value) {
  const n = Math.max(0, Math.round(value || 0))
  if (props.compact && n >= 10000) {
    return `${(n / 10000).toFixed(n >= 100000 ? 0 : 1)}w`
  }
  return n.toLocaleString('zh-CN')
}

function setValue(value, animate = true) {
  tween?.kill()
  if (!animate || reduceMotion()) {
    state.value = Number(value) || 0
    display.value = format(state.value)
    return
  }
  tween = gsap.to(state, {
    value: Number(value) || 0,
    duration: props.duration,
    ease: 'power2.out',
    overwrite: true,
    onUpdate: () => { display.value = format(state.value) }
  })
}

onMounted(() => setValue(props.value))
watch(() => props.value, (value) => setValue(value))
onBeforeUnmount(() => tween?.kill())
</script>

<template>
  <b class="num count-up">{{ display }}</b>
</template>

<style scoped>
.count-up {
  display: block;
  font-variant-numeric: tabular-nums;
  font-feature-settings: 'tnum';
}
</style>
