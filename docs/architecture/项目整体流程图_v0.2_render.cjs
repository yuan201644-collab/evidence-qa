/** 浏览器渲染及文字边界检查。可将可解析的 playwright 包路径作为第一个参数。 */
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const { chromium } = require(process.argv[2] || 'playwright');
(async()=>{
  const stem=process.argv[3] || '项目整体流程图_v0.3';
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:2600,height:2460},deviceScaleFactor:1.5});
    await page.goto(pathToFileURL(path.join(__dirname,stem+'.html')).href+'?export',{waitUntil:'load'});
    await page.evaluate(()=>document.fonts.ready);
    const issues=await page.evaluate(()=>{
      const bad=[];
      for(const t of document.querySelectorAll('text[data-card]')){
        const box=t.getBBox(); const p=document.getElementById(t.dataset.card).dataset.box.split(',').map(Number);
        if(box.x<p[0]+6||box.y<p[1]+3||box.x+box.width>p[0]+p[2]-6||box.y+box.height>p[1]+p[3]-3)
          bad.push({text:t.textContent,box:{x:box.x,y:box.y,w:box.width,h:box.height},card:p});
      }
      return bad;
    });
    if(issues.length)throw new Error(JSON.stringify(issues));
    await page.locator('main svg').screenshot({path:path.join(__dirname,stem+'.png')});
    const info={width:3900,height:3690,cardTextOverflow:issues.length,renderedAt:new Date().toISOString(),font:'Microsoft YaHei',scope:'image layout check, not business functionality test'};
    fs.writeFileSync(path.join(__dirname,stem+'_排版检查.json'),JSON.stringify(info,null,2)+'\n','utf8');
    console.log(JSON.stringify(info));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
