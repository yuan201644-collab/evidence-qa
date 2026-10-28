// 核对界面文案与布局：首页 / 使用指南三步 / 系统说明两视图 / 设置 / 灵感面板 / 事实核验面板。
// 只点与文案有关的入口，不提真实问题（问答要 1–5 分钟，另有脚本）。
const { chromium } = require('playwright')
const fs = require('fs')

const BASE = 'http://127.0.0.1:5184'
const OUT = '../../reports/_ui_shots_r2'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } })
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await sleep(900)

  if (await page.$('input[type="password"]')) {
    await page.locator('input:not([type="password"])').first().fill('admin')
    await page.locator('input[type="password"]').first().fill('Aic2026Demo!')
    await page.locator('button[type="submit"], button:has-text("登录")').first().click()
    await page.waitForLoadState('networkidle')
    await sleep(1600)
  }

  const shot = async (name, label) => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log(`  ${name}.png  ${label || ''}`)
  }

  await shot('01_home', '首页')
  console.log('[首页文案]', (await page.locator('.welcome').innerText()).replace(/\n+/g, ' | ').slice(0, 420))

  // ---- 使用指南三步 ----
  await page.locator('button:has-text("使用指南")').first().click()
  await sleep(800)
  await shot('02_guide_step1', '指南-能答什么')
  console.log('[指南1]', (await page.locator('.guide').innerText()).replace(/\n+/g, ' | ').slice(0, 520))
  await page.locator('.guide__confirm').click()
  await sleep(600)
  await shot('03_guide_step2', '指南-怎么看答案')
  console.log('[指南2]', (await page.locator('.guide__pane').innerText()).replace(/\n+/g, ' | ').slice(0, 520))
  await page.locator('.guide__confirm').click()
  await sleep(600)
  await shot('04_guide_step3', '指南-开始用')
  console.log('[指南3]', (await page.locator('.guide__pane').innerText()).replace(/\n+/g, ' | ').slice(0, 400))
  await page.locator('.guide .close-button').click()
  await sleep(500)

  // ---- 系统说明：流程 / 模块 ----
  await page.locator('button:has-text("系统说明")').first().click()
  await sleep(1400)
  await shot('05_arch_flow', '系统说明-流程')
  console.log('[说明首屏]', (await page.locator('.architecture-dialog').innerText()).replace(/\n+/g, ' | ').slice(0, 460))
  await page.locator('.architecture-tabs button:has-text("模块目录")').click()
  await sleep(900)
  await shot('06_arch_modules', '系统说明-模块')
  // 展开第一个模块（KAG），看「上游能力 · 已连接」那一行
  await page.locator('.architecture-module__toggle').first().click()
  await sleep(500)
  await shot('07_arch_module_open', '模块展开')
  // 上游边界块
  const up = page.locator('.architecture-dialog__upstream')
  if (await up.count()) {
    await up.scrollIntoViewIfNeeded()
    await sleep(400)
    await shot('08_arch_upstream', '上游与自研边界')
    console.log('[上游边界]', (await up.innerText()).replace(/\n+/g, ' | ').slice(0, 420))
  }
  await page.locator('.architecture-dialog .close-button').click()
  await sleep(500)

  // ---- 设置面板 ----
  await page.locator('button:has-text("设置")').first().click()
  await sleep(700)
  await shot('09_settings', '设置')
  console.log('[设置]', (await page.locator('.settings').innerText()).replace(/\n+/g, ' | ').slice(0, 320))
  await page.locator('.settings .close-button').click()
  await sleep(400)

  // ---- 问题灵感面板 ----
  const spark = page.locator('.composer .gallery__trigger, .composer button[aria-label*="问题灵感"]').first()
  if (await spark.count()) {
    await spark.click()
    await sleep(700)
    await shot('10_gallery', '问题灵感')
    console.log('[灵感分组]', (await page.locator('.gallery__pop').innerText()).replace(/\n+/g, ' | ').slice(0, 620))
    await page.locator('.gallery__close').click().catch(() => {})
    await sleep(300)
  } else {
    console.log('!! 没找到灵感按钮')
  }

  // ---- 事实核验面板 ----
  const factBtn = page.locator('button:has-text("事实核验")').first()
  if (await factBtn.count()) {
    await factBtn.click()
    await sleep(2600)
    await shot('11_fact_panel', '事实核验面板')
    console.log('[面板列表头]', (await page.locator('.ft-list').innerText()).replace(/\n+/g, ' | ').slice(0, 400))
  }

  await browser.close()
})().catch((err) => { console.error('FAILED', err); process.exit(1) })
