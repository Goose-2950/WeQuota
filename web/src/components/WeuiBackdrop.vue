<script setup>
/**
 * WeUI 风格动态背景：绿色光晕 + 点阵纹理。
 * 用 GSAP 缓慢漂移，替代静态纯色背景；reduced-motion 下完全静止。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { gsap, reduceMotion } from '../motion'

const orbA = ref(null)
const orbB = ref(null)
const orbC = ref(null)
let timeline = null

onMounted(() => {
  if (reduceMotion()) return
  timeline = gsap.timeline({ repeat: -1, yoyo: true, defaults: { ease: 'sine.inOut' } })
    .to(orbA.value, { x: 60, y: 40, scale: 1.12, duration: 16 }, 0)
    .to(orbB.value, { x: -70, y: 55, scale: 0.94, duration: 21 }, 0)
    .to(orbC.value, { x: 45, y: -60, scale: 1.08, duration: 18 }, 0)
})

onBeforeUnmount(() => timeline?.kill())
</script>

<template>
  <div class="weui-backdrop" aria-hidden="true">
    <span ref="orbA" class="backdrop-orb orb-a"></span>
    <span ref="orbB" class="backdrop-orb orb-b"></span>
    <span ref="orbC" class="backdrop-orb orb-c"></span>
    <span class="backdrop-grid"></span>
  </div>
</template>

<style scoped>
.weui-backdrop {
  position: fixed;
  inset: 0;
  z-index: 0;
  overflow: hidden;
  pointer-events: none;
  background: var(--bg);
}
.backdrop-orb {
  position: absolute;
  display: block;
  border-radius: 50%;
  filter: blur(80px);
  opacity: 0.55;
  will-change: transform;
}
.orb-a {
  top: -24vw;
  left: -16vw;
  width: 62vw;
  height: 62vw;
  background: radial-gradient(
    circle,
    color-mix(in srgb, var(--accent) 24%, transparent),
    transparent 70%
  );
}
.orb-b {
  top: 8vh;
  right: -18vw;
  width: 46vw;
  height: 46vw;
  opacity: 0.4;
  background: radial-gradient(
    circle,
    color-mix(in srgb, #10aeff 19%, transparent),
    transparent 72%
  );
}
.orb-c {
  bottom: -32vw;
  left: 18vw;
  width: 60vw;
  height: 60vw;
  opacity: 0.38;
  background: radial-gradient(
    circle,
    color-mix(in srgb, var(--accent-strong) 23%, transparent),
    transparent 72%
  );
}
.backdrop-grid {
  position: absolute;
  inset: 0;
  opacity: 0.42;
  background-image: radial-gradient(
    circle,
    color-mix(in srgb, var(--text) 8%, transparent) 1px,
    transparent 1px
  );
  background-size: 24px 24px;
  -webkit-mask-image: radial-gradient(ellipse 86% 74% at 56% 20%, #000 4%, transparent 76%);
  mask-image: radial-gradient(ellipse 86% 74% at 56% 20%, #000 4%, transparent 76%);
}
@media (prefers-reduced-motion: reduce) {
  .backdrop-orb { opacity: 0.35; }
}
</style>
