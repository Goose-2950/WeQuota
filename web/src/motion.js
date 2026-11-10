/** 全局 GSAP 配置：页面入场、滚动揭示、数字滚动与平滑定位共用。 */
import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'
import { ScrollToPlugin } from 'gsap/ScrollToPlugin'

gsap.registerPlugin(ScrollTrigger, ScrollToPlugin)

export const reduceMotion = () =>
  typeof window !== 'undefined' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

export function scrollToTarget(target, offsetY = 84) {
  if (!target) return
  if (reduceMotion()) {
    target.scrollIntoView({ block: 'start' })
    return
  }
  gsap.to(window, {
    duration: 0.75,
    ease: 'power3.inOut',
    scrollTo: { y: target, offsetY }
  })
}

export { gsap, ScrollTrigger }
