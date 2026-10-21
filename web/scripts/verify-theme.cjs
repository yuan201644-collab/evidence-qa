/* 暗色模式实跑验证：真实浏览器渲染两种主题并截图，检查可读性与残留浅色块。
 *
 * 为什么必须实跑而不是只看构建：token 替换的错误形式是"浅色文字留在浅色底上"，
 * 这类问题 CSS 语法完全合法、构建和类型检查一律通过，只有在真实渲染里量对比度
 * 才能发现。所以这里对若干关键选择器采样计算前后的对比度。
 *
 * 用法：node scripts/verify-theme.cjs [--live]
 *   （默认打 dev server；--live 时会走真实 /api/ask 再截图证据面板）
 */
const { chromium } = require('playwright')
const fs = require('node:fs')
const path = require('node:path')

const BASE = process.env.UI_BASE_URL || 'http://127.0.0.1:5184'
const OUT = process.env.UI_OUTPUT_DIR || path.join('..', 'agent_temp', 'theme_check')
const CHANNEL = process.env.UI_BROWSER_CHANNEL || 'msedge'

/** 相对亮度，用于算对比度（WCAG 2.x 定义）。 */
function luminance([r, g, b]) {
  const channel = (value) => {
    const v = value / 255
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

/** 在页面里把每个选择器的前景/背景色解析成 rgb 三元组。
 *  传函数对象而非字符串：字符串形式的箭头函数在部分 Playwright 版本下会被当成
 *  表达式求值，结果直接序列化成 undefined。 */
function SAMPLE(selectors) {
  function parse(color) {
    const m = color.match(/rgba?\(([^)]+)\)/)
    if (!m) return null
    const parts = m[1].split(',').map((s) => parseFloat(s.trim()))
    return { rgb: [parts[0], parts[1], parts[2]], alpha: parts.length > 3 ? parts[3] : 1 }
  }
  // 背景可能是透明的，需要向上找第一个不透明的祖先背景。
  function effectiveBg(el) {
    let node = el
    while (node && node !== document.documentElement) {
      const bg = parse(getComputedStyle(node).backgroundColor)
      if (bg && bg.alpha > 0.9) return bg.rgb
      node = node.parentElement
    }
    const bodyBg = parse(getComputedStyle(document.body).backgroundColor)
    return bodyBg ? bodyBg.rgb : [255, 255, 255]
  }
  return selectors.map((selector) => {
    const el = document.querySelector(selector)
    if (!el) return { selector, found: false }
    const style = getComputedStyle(el)
    const fg = parse(style.color)
    return {
      selector,
      found: true,
      fg: fg && fg.alpha > 0.05 ? fg.rgb : null,
      bg: effectiveBg(el),
      text: (el.textContent || '').trim().slice(0, 24),
    }
  })
}

const TARGETS = [
  'body',
  '.sidebar',
  '.brand b',
  '.new-session',
  '.session-select',
  '.workspace-label',
  '.topbar',
  '.breadcrumb b',
  '.architecture-button',
  '.theme-button',
  '.welcome h1',
  '.welcome__description',
  '.example-card',
  '.example-card__question',
  '.composer',
  'textarea',
  '.composer-note',
  '.source-scope__item',
  '.welcome-flow li',
  '.service-state',
  '.local-note',
]

async function main() {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: CHANNEL })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()) })
  // 记下具体 URL：只知道"有个 404"没法判断是资源缺失还是 HMR 的正常抖动。
  const badResponses = []
  page.on('response', (response) => { if (response.status() >= 400) badResponses.push(`${response.status()} ${response.url()}`) })

  await page.route('**/api/auth/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ user: { id: 'ui-check-user', username: 'ui-check-user', role: 'user', active: true, created_at: '2026-10-14T00:00:00Z' } }),
  }))
  await page.route('**/api/models', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ default_model_id: 'deepseek-v4-1-flash', models: [{ id: 'deepseek-v4-1-flash', label: 'DeepSeek-V4.1-Flash', provider: '火山方舟', model: 'deepseek-v4.1-flash', configured: true, selected: true }] }),
  }))
  await page.route('**/api/database/status', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ status: 'connected', database: 'theme-fixture', catalog_database: 'theme-fixture', label: '主题检查数据源', dialect: 'postgres', table_count: 1, checked_at: '2026-10-14T00:00:00Z' }),
  }))
  await page.route('**/api/database/unstructured/status', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ status: 'memory', backend: 'memory', label: '主题检查文档源', checked_at: '2026-10-14T00:00:00Z' }),
  }))
  await page.route('**/api/capabilities/cross-source-demo', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ status: 'unavailable' }),
  }))
  await page.route('**/api/kag/status', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ ready: false, status: 'not_configured', reason_code: 'runtime_not_configured' }),
  }))

  // 新用户即使操作系统是暗色，也应以亮色作为产品默认主题。
  await page.emulateMedia({ colorScheme: 'dark' })
  await page.addInitScript(() => localStorage.removeItem('askdata-theme'))
  await page.goto(BASE, { waitUntil: 'networkidle' })
  const defaultTheme = await page.evaluate(() => document.documentElement.dataset.theme)
  if (defaultTheme !== 'light') throw new Error(`未设置主题偏好时应默认亮色，实际为 ${defaultTheme}`)
  await page.waitForTimeout(100)
  const stableDefaultTheme = await page.evaluate(() => document.documentElement.dataset.theme)
  if (stableDefaultTheme !== 'light') throw new Error(`默认亮色被系统暗色覆盖，实际为 ${stableDefaultTheme}`)
  console.log('  default theme @system-dark: light')

  const report = { base: BASE, generated_at: new Date().toISOString(), default_theme: defaultTheme, themes: {}, errors, bad_responses: badResponses }

  for (const theme of ['light', 'dark']) {
    // 必须走真实点击切换，不能直接改 <html> 的 data-theme：
    // 后者绕过了 Vue 的响应式状态，按钮图标会停留在初始值，测出来的
    // 是"CSS 应用了但组件没更新的"假象。
    const switched = await page.evaluate((next) => {
      if (document.documentElement.dataset.theme === next) return 'already'
      const button = document.querySelector('.theme-button')
      if (!button) return 'no-button'
      button.click()
      return 'clicked'
    }, theme)
    await page.waitForTimeout(320)

    const applied = await page.evaluate(() => document.documentElement.dataset.theme)
    if (applied !== theme) throw new Error(`切换主题失败：期望 ${theme}，实际 ${applied}（${switched}）`)

    // 图标与文案必须跟着主题走：这类错误颜色全对，只有断言能抓。
    const chrome = await page.evaluate(() => {
      const button = document.querySelector('.theme-button')
      const label = button ? button.getAttribute('aria-label') : null
      const paths = button ? Array.from(button.querySelectorAll('path')).map((p) => p.getAttribute('d')).join('|') : ''
      return { label, paths }
    })
    const expectSun = theme === 'dark'
    const looksSun = chrome.paths.includes('M12 17a5 5 0')
    const looksMoon = chrome.paths.includes('M20 14.5A8.5')
    if (expectSun && !looksSun) throw new Error(`${theme} 主题下按钮应显示太阳图标（点击切回亮色），实际未匹配`)
    if (!expectSun && !looksMoon) throw new Error(`${theme} 主题下按钮应显示月亮图标（点击切到暗色），实际未匹配`)
    console.log(`  theme-button @${theme}: aria-label="${chrome.label}" icon=${looksSun ? 'sun' : 'moon'}`)

    const samples = await page.evaluate(SAMPLE, TARGETS)

    const rows = samples.map((sample) => {
      if (!sample.found) return { ...sample, contrast: null, verdict: 'not-rendered' }
      if (!sample.fg) return { ...sample, contrast: null, verdict: 'no-text-color' }
      const l1 = luminance(sample.fg)
      const l2 = luminance(sample.bg)
      const contrast = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
      return { ...sample, contrast: Number(contrast.toFixed(2)), verdict: contrast >= 4.5 ? 'ok' : contrast >= 3 ? 'low' : 'fail' }
    })

    report.themes[theme] = { applied, rows }
    await page.screenshot({ path: path.join(OUT, `theme-${theme}-welcome.png`), fullPage: false })
  }

  // 暗色 + 会话态：欢迎页是纯静态的，真实风险在答案气泡/引用/证据这些有底色的组件上。
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })
  await page.waitForTimeout(150)
  const chatTargets = ['.bubble', '.answer-head > b', '.answer-type', '.answer-label', '.content', '.citations header', '.citation', '.citation__body b', '.citation__body p', '.citation__meta']
  await page.evaluate(() => {
    // 直接注入一段可控 DOM 来验证组件样式，不依赖后端。
    const host = document.createElement('div')
    host.innerHTML = `
      <article class="bubble assistant"><header class="answer-head"><span class="answer-mark"></span><b>知证</b><span class="answer-type">文档问答</span></header><p class="answer-label">回答</p><p class="content">质保期为自设备验收合格之日起 36 个月 [1]。</p><section class="citations"><header><span>回答依据</span><span class="citation-tip">点击查看对应原文或数据记录</span></header><div class="citation-grid"><button class="citation"><span class="citation__number">1</span><div class="citation__body"><b>智能计算设备年度维保服务合同</b><span class="citation__meta">第 1 页 · 第二章 质保条款</span><p>质保期为自设备验收合格之日起 36 个月…</p></div></button></div></section></article>`
    host.id = 'theme-probe'
    document.body.appendChild(host)
  })
  await page.waitForTimeout(150)
  const chatRows = (await page.evaluate(SAMPLE, chatTargets)).map((sample) => {
    if (!sample.found || !sample.fg) return { ...sample, contrast: null, verdict: 'n/a' }
    const l1 = luminance(sample.fg)
    const l2 = luminance(sample.bg)
    const contrast = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
    return { ...sample, contrast: Number(contrast.toFixed(2)), verdict: contrast >= 4.5 ? 'ok' : contrast >= 3 ? 'low' : 'fail' }
  })
  report.themes.dark_chat = { rows: chatRows }
  await page.screenshot({ path: path.join(OUT, 'theme-dark-chat.png'), fullPage: false })

  // token 完整性：颜色类 token 必须两套主题都有值。只比对颜色——
  // 圆角半径这类几何量与主题无关，不该要求暗色变体，否则误报。
  // motion 同理：动效时长/缓动/位移是节奏参数，切主题不该变，列进来免挨误报。
  const GEOMETRY = /^--(radius|sidebar|font|space|size|width|height|z-|motion)/
  const tokenAudit = await page.evaluate((geometrySource) => {
    const geometry = new RegExp(geometrySource)
    const sheets = Array.from(document.styleSheets)
    function collect(predicate) {
      const names = new Set()
      for (const sheet of sheets) {
        let rules
        try { rules = Array.from(sheet.cssRules) } catch { continue }
        for (const rule of rules) {
          if (!rule.style || !predicate(rule.selectorText || '')) continue
          for (const property of Array.from(rule.style)) {
            if (property.startsWith('--')) names.add(property)
          }
        }
      }
      return names
    }
    const light = collect((selector) => selector === ':root')
    const dark = collect((selector) => selector.includes('dark'))
    const missing = Array.from(light).filter((name) => !dark.has(name) && !geometry.test(name))
    return { light: light.size, dark: dark.size, missing }
  }, GEOMETRY.source)
  report.token_audit = tokenAudit
  console.log(`\ntokens: light=${tokenAudit.light} dark=${tokenAudit.dark}`)
  if (tokenAudit.missing.length) console.log('  缺暗色值（颜色类）: ' + tokenAudit.missing.join(', '))
  else console.log('  每个颜色类 token 都有暗色值')

  fs.writeFileSync(path.join(OUT, 'theme-checks.json'), JSON.stringify(report, null, 2))

  // 报告
  for (const [theme, data] of Object.entries(report.themes)) {
    console.log(`\n=== ${theme} ===`)
    for (const row of data.rows || []) {
      const flag = row.verdict === 'ok' ? '  ' : '!!'
      console.log(`${flag} ${String(row.contrast ?? '-').padStart(6)}  ${row.verdict.padEnd(13)} ${row.selector}`)
    }
  }
  const failures = Object.values(report.themes).flatMap((d) => d.rows).filter((r) => r.verdict === 'fail')
  console.log(`\nfailures: ${failures.length}`)
  for (const f of failures) console.log(`  !! ${f.selector}  contrast=${f.contrast}  fg=${f.fg} bg=${f.bg}`)
  if (errors.length) console.log(`\nbrowser errors: ${errors.length}\n` + errors.slice(0, 10).join('\n'))
  if (badResponses.length) console.log(`\nbad responses:\n` + badResponses.join('\n'))

  await browser.close()
  process.exit(failures.length || errors.length || tokenAudit.missing.length ? 1 : 0)
}

main().catch((error) => { console.error(error); process.exit(1) })
