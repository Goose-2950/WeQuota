<script setup>
/** 极简 ECharts 封装：主题色由调用方传入，自适应尺寸，卸载时销毁。 */
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkLineComponent,
  TitleComponent
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkLineComponent,
  TitleComponent,
  CanvasRenderer
])

const props = defineProps({
  option: { type: Object, required: true },
  height: { type: String, default: '260px' }
})

const el = ref(null)
let chart = null
let observer = null

const render = () => {
  if (!chart) return
  chart.setOption(props.option, true)
}

onMounted(() => {
  chart = echarts.init(el.value, null, { renderer: 'canvas' })
  render()
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(el.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})

watch(() => props.option, render, { deep: true })
</script>

<template>
  <div ref="el" :style="{ width: '100%', height }"></div>
</template>
