// 只截首页与事实面板（不提问题），用于核对文案与布局改动。
const { chromium } = require('playwright')
const fs = require('fs')

const BASE = 'http://127.0.0.1:5184'
const OUT = '../../reports/_ui_shots'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await sleep(1000)
  if (await page.$('input[type="password"]')) {
    await page.locator('input:not([type="password"])').first().fill('admin')
    await page.locator('input[type="password"]').first().fill('Aic2026Demo!')
    await page.locator('button[type="submit"], button:has-text("登录")').first().click()
    await page.waitForLoadState('networkidle')
    await sleep(1500)
  }
  await page.screenshot({ path: `${OUT}/01_home.png` })
  console.log('01_home.png ok')
  const welcome = await page.locator('.welcome').innerText().catch(() => '')
  console.log('--- 欢迎区 ---')
  console.log(welcome.slice(0, 500))

  const factBtn = page.locator('button:has-text("事实核验")').first()
  if (await factBtn.count()) {
    await factBtn.click()
    await sleep(2200)
    await page.screenshot({ path: `${OUT}/02_fact_panel.png` })
    console.log('02_fact_panel.png ok')
  }
  await browser.close()
})().catch((err) => { console.error('FAILED', err); process.exit(1) })
