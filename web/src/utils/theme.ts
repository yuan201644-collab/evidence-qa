/** 主题（亮色/暗色）状态管理与持久化。
 *
 * 三条设计约束：
 *
 * 1. 首屏不能闪白。默认主题在 <html> 上由 index.html 里的内联脚本抢先设定，
 *    不等 Vue 挂载。如果等这里初始化再改，暗色用户会先看到一帧白屏。
 *    这就是为什么 THEME_STORAGE_KEY 和那段内联脚本里的字符串必须一致。
 *
 * 2. 未设置偏好时默认亮色。只有显式选择“跟随系统”才跟随操作系统；
 *    存储值为 'light' | 'dark' | 'system'，null 表示“使用默认亮色”。
 *
 * 3. 显式跟随系统时要监听主题变化；亮色默认值和手动选择不受系统变化影响。
 */

import { computed, readonly, ref } from 'vue'

export type Theme = 'light' | 'dark'
type ThemePreference = Theme | 'system' | null

export const THEME_STORAGE_KEY = 'askdata-theme'

function readStored(): ThemePreference {
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY)
    return value === 'light' || value === 'dark' || value === 'system' ? value : null
  } catch {
    // 隐私模式或存储被禁用时 localStorage 会抛，此时使用默认亮色。
    return null
  }
}

function systemPrefersDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: dark)').matches
}

const stored = ref<ThemePreference>(readStored())
const theme = ref<Theme>(
  stored.value === 'light' || stored.value === 'dark'
    ? stored.value
    : (stored.value === 'system' && systemPrefersDark() ? 'dark' : 'light'),
)

/** 当前是否显式选择了跟随系统。 */
export const followsSystem = computed(() => stored.value === 'system')

function apply(next: Theme) {
  theme.value = next
  const root = document.documentElement
  root.dataset.theme = next
  // 同步 meta[name=theme-color]，让移动端浏览器地址栏跟着变色。
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute('content', next === 'dark' ? '#14181f' : '#315bc7')
  window.dispatchEvent(new CustomEvent('theme-change', { detail: next }))
}

/** 在亮色/暗色之间切换。一旦手动切换，就不再跟随系统。 */
export function toggleTheme() {
  const next: Theme = theme.value === 'dark' ? 'light' : 'dark'
  stored.value = next
  try {
    localStorage.setItem(THEME_STORAGE_KEY, next)
  } catch {
    // 存不下也不影响本次生效，只是刷新后恢复默认亮色。
  }
  apply(next)
}

/** 显式切换到"跟随系统"。 */
export function useSystemTheme() {
  stored.value = 'system'
  try {
    localStorage.setItem(THEME_STORAGE_KEY, 'system')
  } catch {
    // 同上，忽略。
  }
  apply(systemPrefersDark() ? 'dark' : 'light')
}

/** 在 main.ts 里调用一次：接管系统主题变化的监听。 */
export function initTheme() {
  // index.html 的内联脚本已经设过 data-theme；这里把它与响应式状态对齐，
  // 避免两边不一致（例如用户清缓存后内联脚本读到的是另一份存储值）。
  apply(theme.value)
  const query = window.matchMedia('(prefers-color-scheme: dark)')
  query.addEventListener('change', (event) => {
    if (stored.value !== 'system') return // 默认亮色/手动主题不受系统变化干预
    apply(event.matches ? 'dark' : 'light')
  })
}

export function useTheme() {
  return { theme: readonly(theme), followsSystem, toggleTheme, useSystemTheme }
}
