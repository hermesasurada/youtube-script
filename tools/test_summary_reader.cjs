// NODE_PATH=<bundled node_modules> node tools/test_summary_reader.cjs
// Isolated browser fixture: no production API requests or user-data writes.
const { chromium } = require('playwright');
const path = require('path');
const assert = require('node:assert/strict');
const base = path.resolve(__dirname, '..');
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 800}});
    const notes = {};
    await page.route('http://reader.test/**', async route => {
      if (route.request().url().endsWith('/summary/note')) {
        const data = route.request().postDataJSON();
        notes[data.section_key] = data.body.trim();
        return route.fulfill({json: {body: data.body.trim()}});
      }
      return route.fulfill({contentType: 'text/html', body: '<html><head></head><body><main class="sum-md" id="reader" style="height:600px;overflow:auto;background:white;padding:0 20px"></main></body></html>'});
    });
    await page.goto('http://reader.test/');
    await page.addScriptTag({path: path.join(base, 'static/vendor/marked.min.js')});
    await page.addScriptTag({path: path.join(base, 'static/js/common.js')});
    await page.evaluate(() => {
      window.sample = '# 테스트 요약\n\n## 3. 핵심 내용\n\n### 전력 & 투자 [00:01]\n\nERCOT\\* 첫 단락.\n\nELCC\\* 둘째 단락.\n\n<div class="term-notes"><p class="term-note">* <strong>ERCOT (Electric Reliability Council of Texas)</strong> — 전력망 운영기관</p><p class="term-note">* <strong>ELCC (Effective Load Carrying Capability)</strong> — 용량 지표</p></div>\n\n### 다음 [01:00]\n\n마지막 단락.';
      window.render = notes => {
        const root = document.querySelector('#reader');
        root.innerHTML = YS.renderMarkdown(sample);
        YS.stripSummaryPopupChrome(root);
        YS.setupStickySummarySections(root);
        YS.setSummaryNotes(1, notes);
        YS.attachSummaryNotes(root, 1);
      };
      render({});
    });
    assert.deepEqual(await page.locator('.term-notes').evaluateAll(boxes => boxes.map(b => b.previousElementSibling.textContent)), ['ERCOT* 첫 단락.', 'ELCC* 둘째 단락.']);
    await page.getByRole('button', {name: '+ 내 의견 메모'}).first().click();
    await page.getByRole('textbox', {name: '나의 의견 메모'}).fill('내 생각 <script> & 비교\n두 번째 줄');
    await page.getByRole('button', {name: '저장', exact: true}).click();
    await page.getByRole('button', {name: '메모 편집'}).waitFor();
    assert.equal(notes['전력 & 투자::1'], '내 생각 <script> & 비교\n두 번째 줄');
    await page.evaluate(notes => render(notes), notes);
    assert.equal(await page.locator('.summary-memo-text').textContent(), notes['전력 & 투자::1']);
    assert.equal(await page.locator('.summary-memo script').count(), 0);
    await page.getByRole('button', {name: '메모 편집'}).click();
    await page.getByRole('textbox').fill('수정한 의견');
    await page.getByRole('button', {name: '저장', exact: true}).click();
    await page.getByRole('button', {name: '메모 편집'}).waitFor();
    assert.equal(notes['전력 & 투자::1'], '수정한 의견');
    const exported = await page.evaluate(() => {
      const box = document.createElement('div'); box.innerHTML = YS.mdToBloggerHtml(sample).html;
      return {keys: [...box.querySelectorAll('h3')].map(h => h.dataset.summarySection),
        paragraphs: [...box.querySelectorAll('.term-notes')].map(b => b.previousElementSibling.textContent)};
    });
    assert.deepEqual(exported.keys, ['전력 & 투자::1', '다음::1']);
    assert.deepEqual(exported.paragraphs, ['ERCOT* 첫 단락.', 'ELCC* 둘째 단락.']);
    await page.setViewportSize({width:390, height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path:'/tmp/youtube-summary-memo-mobile.png', fullPage:true});
    await page.getByRole('button', {name: '메모 편집'}).click();
    await page.getByRole('textbox').fill('');
    await page.getByRole('button', {name: '저장', exact: true}).click();
    await page.getByRole('button', {name: '+ 내 의견 메모'}).first().waitFor();
    assert.equal(await page.locator('.summary-memo-text').count(), 0);
    const sticky = await page.evaluate(() => {
      const root = document.querySelector('#reader');
      const head = root.querySelector('.sum-head-sticky');
      head.style.cssText = 'position:sticky;top:0;background:white;z-index:10';
      const first = root.querySelector('.sum-topic-section');
      first.querySelector('p').style.height = '1000px';
      root.style.height = '400px';
      root.scrollTop = 250;
      const h = first.querySelector('h3');
      return {gap: h.getBoundingClientRect().top - head.getBoundingClientRect().bottom,
        position: getComputedStyle(h).position};
    });
    assert.equal(sticky.position, 'sticky');
    assert.ok(Math.abs(sticky.gap - 8) < 2, JSON.stringify(sticky));
    console.log('PASS: paragraph notes, memo create/edit/reopen/delete, safe text, export keys, mobile width');
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode = 1;});
