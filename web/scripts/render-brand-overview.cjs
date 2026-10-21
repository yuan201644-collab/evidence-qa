const path = require('node:path')
const { pathToFileURL } = require('node:url')
const { chromium } = require('playwright')

const overviewPath = path.resolve(__dirname, '../../docs/brand/图标总览.html')
const outputPath = path.resolve(__dirname, '../../docs/brand/图标总览.png')

async function main() {
  const channel = process.env.UI_BROWSER_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined)
  const browser = await chromium.launch({ channel, headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 1 })
    await page.goto(pathToFileURL(overviewPath).href, { waitUntil: 'load' })
    await page.evaluate(() => document.fonts.ready)
    const brokenImages = await page.locator('img').evaluateAll((images) => images
      .filter((image) => !image.complete || image.naturalWidth === 0)
      .map((image) => image.getAttribute('src')))
    if (brokenImages.length) throw new Error(`图标总览存在无法加载的图像：${brokenImages.join(', ')}`)
    await page.screenshot({ path: outputPath, fullPage: true })
    process.stdout.write(`${JSON.stringify({ status: 'ok', overviewPath, outputPath, imageCount: await page.locator('img').count() })}\n`)
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
