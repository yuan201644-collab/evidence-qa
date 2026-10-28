// 端到端：真实提一次问（M-17，约 1 分钟），截答案卡、事实核验卡、点证据块后的证据面板。
// 只走界面，不绕过前端 —— 这是演示路径本身，能跑通才算交付。
const { chromium } = require('playwright')
const fs = require('fs')

const BASE = 'http://127.0.0.1:5184'
const OUT = '../../reports/_ui_shots_r2'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.setDefaultTimeout(260000)
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await sleep(800)

  if (await page.$('input[type="password"]')) {
    await page.locator('input:not([type="password"])').first().fill('admin')
    await page.locator('input[type="password"]').first().fill('Aic2026Demo!')
    await page.locator('button[type="submit"], button:has-text("登录")').first().click()
    await page.waitForLoadState('networkidle')
    await sleep(1500)
  }

  // 直接填输入框发送，避免示例卡的 disabled 判断影响
  const box = page.locator('textarea[aria-label="输入问题"]').first()
  await box.fill('2016 年度山东省高校科研发展计划自筹经费项目名单中，中国石油大学（华东）的项目有几项？分别属于哪些类别？')
  await sleep(300)
  await page.locator('.composer button[type="submit"], .composer button:has-text("发送")').first().click()
  console.log('已发送，等待答案…')

  await page.locator('.fact-review').first().waitFor({ state: 'visible', timeout: 250000 })
  await sleep(1200)
  await page.screenshot({ path: `${OUT}/12_answer.png`, fullPage: false })
  console.log('12_answer.png ok')

  const review = await page.locator('.fact-review').first().innerText().catch(() => '')
  console.log('[事实卡]', review.replace(/\n+/g, ' | ').slice(0, 600))

  const answer = await page.locator('.content').last().innerText().catch(() => '')
  console.log('[答案]', answer.replace(/\n+/g, ' | ').slice(0, 400))

  // 点事实卡里的第一个证据按钮 → 右侧来源证据面板
  const ev = page.locator('.fact-review__source').first()
  if (await ev.count()) {
    await ev.click()
    await sleep(1600)
    await page.screenshot({ path: `${OUT}/13_evidence.png` })
    console.log('13_evidence.png ok')
    const panel = await page.locator('.evidence, .doc-fallback').first().innerText().catch(() => '')
    console.log('[证据面板]', panel.replace(/\n+/g, ' | ').slice(0, 380))
  } else {
    console.log('!! 事实卡里没有证据按钮')
  }

  // 处理详情（trace 三步的标签是否还有「工具调用」兜底）
  const detail = page.locator('button:has-text("查看处理详情"), button:has-text("处理详情")').first()
  if (await detail.count()) {
    await detail.click()
    await sleep(1200)
    await page.screenshot({ path: `${OUT}/14_trace.png` })
    const txt = await page.locator('.trace, .trace-drawer').first().innerText().catch(() => '')
    console.log('[处理详情]', txt.replace(/\n+/g, ' | ').slice(0, 300))
  } else {
    console.log('!! 没找到处理详情入口')
  }

  await browser.close()
})().catch((err) => { console.error('FAILED', err); process.exit(1) })
