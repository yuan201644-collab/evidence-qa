// 界面对拍脚本：登录 → 首页 → 事实核验面板 → 提一次问 → 截答案卡。
// 目的不是自动化测试，是拿到能肉眼核对的截图，确认「事实表三列」真的渲染出来了。
const { chromium } = require('playwright')
const fs = require('fs')

const BASE = 'http://127.0.0.1:5184'
const OUT = '../../reports/_ui_shots'
const USER = 'admin'
const PASS = 'Aic2026Demo!'
const QUESTION = process.argv[2] || '2026 年度名单的项目总数比 2023 年度增加了多少项？'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  // 本机只装了 headless-shell（`npx playwright install chromium` 默认下载的就是它），
  // 完整 chromium 目录不存在，所以**不要**传 executablePath，否则会去点一个空路径。
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
  page.on('console', (m) => { if (m.type() === 'error') console.log('[console.error]', m.text()) })

  await page.goto(BASE, { waitUntil: 'networkidle' })
  await sleep(1200)

  // 登录页：打印可见输入控件，便于定位（不靠猜选择器）
  const inputs = await page.$$eval('input', (els) =>
    els.map((e) => ({ type: e.type, name: e.name, placeholder: e.placeholder, id: e.id })))
  console.log('inputs:', JSON.stringify(inputs))
  if (await page.$('input[type="password"]')) {
    const text = page.locator('input:not([type="password"])').first()
    await text.fill(USER)
    await page.locator('input[type="password"]').first().fill(PASS)
    await page.locator('button[type="submit"], button:has-text("登录")').first().click()
    await page.waitForLoadState('networkidle')
    await sleep(1500)
  }

  await page.screenshot({ path: `${OUT}/01_home.png`, fullPage: false })
  console.log('01_home.png ok')

  // 事实核验面板
  const factBtn = page.locator('button:has-text("事实核验")').first()
  if (await factBtn.count()) {
    await factBtn.click()
    await sleep(2500)
    await page.screenshot({ path: `${OUT}/02_fact_panel.png`, fullPage: false })
    console.log('02_fact_panel.png ok')
    const body = await page.locator('.ft-main').innerText().catch(() => '')
    console.log('panel text:', body.slice(0, 400))
    await page.keyboard.press('Escape')
    await sleep(500)
  } else {
    console.log('!! 没找到「事实核验」入口按钮')
  }

  // 提一次问
  const box = page.locator('textarea, input[type="text"]').last()
  await box.click()
  await box.fill(QUESTION)
  await page.keyboard.press('Enter')
  console.log('已提交，等待答案…')
  const started = Date.now()
  // 等事实核验卡片出现（它就是答案卡里的事实表），最多等 9 分钟
  try {
    await page.locator('.fact-review').first().waitFor({ timeout: 540000 })
    console.log('答案已返回，用时 %ds', Math.round((Date.now() - started) / 1000))
  } catch (err) {
    console.log('!! 等待答案超时或未出现事实卡：', err.message.slice(0, 120))
  }
  await sleep(1500)
  await page.screenshot({ path: `${OUT}/03_answer.png`, fullPage: true })
  console.log('03_answer.png ok')

  const factText = await page.locator('.fact-review').first().innerText().catch(() => '')
  console.log('--- 事实卡文本 ---')
  console.log(factText.slice(0, 900))
  const answerText = await page.locator('.answer-bubble, .answer').first().innerText().catch(() => '')
  console.log('--- 答案文本 ---')
  console.log(answerText.slice(0, 400))

  await browser.close()
})().catch((err) => { console.error('FAILED', err); process.exit(1) })
