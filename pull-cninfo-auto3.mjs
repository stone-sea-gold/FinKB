#!/usr/bin/env node
/**
 * 拉取 赛力斯(601127) / 比亚迪(002594) / 长安汽车(000625) 2025、2026 年度已披露的定期报告。
 *
 * 为什么不用 MCP 工具的返回值直接落库：
 *  CNINFO 搜索接口有「HTTP 200 但返回空集」的抖动（实测 25%~60%），
 *  MCP 工具会把空集当成"没有这份报告"返回（count: 0）。
 *  实测 601127 的 2025 半年报/三季报就是这样被漏掉的，故此处按标题规则自行重试直到命中。
 */
import { mkdir, writeFile } from 'node:fs/promises';

const MOD = '/home/generalblack/.npm/_npx/b5abb3253bb86cf1/node_modules/@duhu2000/cninfo-mcp/lib/cninfo.mjs';
const { internals, isReportTitle, resolveAttachmentUrl } = await import(MOD);

const OUT = '/home/generalblack/code/works/reporting/raw';
const CODES = ['601127', '002594', '000625'];
const YEARS = [2025, 2026];
const TYPES = [['annual', '年报'], ['semiannual', '半年报'], ['q1', '一季报'], ['q3', '三季报']];

const TODAY = new Date().toISOString().slice(0, 10);
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36';
const QUERY_URL = 'https://www.cninfo.com.cn/new/hisAnnouncement/query';
const MAX_ATTEMPTS = 6;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const branchOf = (c) => (/^6/.test(c) ? { column: 'sse', plate: 'sh' } : { column: 'szse', plate: 'sz' });

async function queryOnce(code, type, year) {
  const b = branchOf(code);
  const payload = internals.reportQuery(1, code, type, b.column, b.plate, '');
  payload.seDate = `${year}-01-01~${TODAY}`; // seDate 按公告日过滤，半年报/三季报都在当年公告
  const res = await fetch(QUERY_URL, {
    method: 'POST',
    headers: {
      accept: 'application/json, text/javascript, */*; q=0.01',
      'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
      origin: 'https://www.cninfo.com.cn',
      referer: 'https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice',
      'x-requested-with': 'XMLHttpRequest',
      'user-agent': UA,
    },
    body: new URLSearchParams(Object.entries(payload).map(([k, v]) => [k, String(v ?? '')])),
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const json = await res.json();
  const list = Array.isArray(json.announcements) ? json.announcements : [];
  return list
    .filter((x) => String(x.secCode) === code && isReportTitle(x.announcementTitle, type, year))
    .map((x) => ({ ...x, pdfUrl: resolveAttachmentUrl(x.adjunctUrl) }))
    .sort((a, b) => (b.announcementTime || 0) - (a.announcementTime || 0));
}

/** 空集不等于不存在：重试到底再下结论 */
async function queryWithRetry(code, type, year, max = MAX_ATTEMPTS) {
  for (let i = 1; i <= max; i++) {
    try {
      const hits = await queryOnce(code, type, year);
      if (hits.length || i === max) return { hits, attempts: i };
    } catch (e) {
      if (i === max) return { hits: [], attempts: i, error: e.message };
    }
    await sleep(500 + Math.random() * 600);
  }
  return { hits: [], attempts: max };
}

async function download(url, path) {
  const res = await fetch(url, {
    headers: { 'user-agent': UA, referer: 'https://www.cninfo.com.cn/', accept: 'application/pdf,*/*' },
    signal: AbortSignal.timeout(120_000),
  });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const buf = Buffer.from(await res.arrayBuffer());
  if (buf.subarray(0, 5).toString('latin1') !== '%PDF-') throw new Error(`不是有效 PDF (${buf.length}B)`);
  await writeFile(path, buf);
  return buf.length;
}

await mkdir(OUT, { recursive: true });
const rows = [];

for (const code of CODES) {
  for (const year of YEARS) {
    for (const [type, label] of TYPES) {
      if (year === 2026 && type === 'annual') {
        rows.push({ code, year, label, status: '尚未发布', note: '法定披露截止 2027-04-30' });
        continue;
      }
      const r = await queryWithRetry(code, type, year);
      await sleep(300);
      if (!r.hits.length) {
        const pending = year === 2026 && type === 'q3';
        rows.push({
          code, year, label, status: pending ? '尚未披露' : '未获取',
          note: r.error ? '请求异常: ' + r.error
            : pending ? `法定截止 2026-10-31，${r.attempts} 次查询均无记录` : `重试 ${r.attempts} 次仍空`,
        });
        console.log(`  ✗ ${code} ${year} ${label}  ${pending ? '尚未披露' : '未获取'}`);
        continue;
      }
      const best = r.hits[0];
      const fname = `${code}_${year}_${label}.pdf`;
      try {
        const bytes = await download(best.pdfUrl, `${OUT}/${fname}`);
        rows.push({
          code, year, label, status: '已下载', secName: best.secName, title: best.announcementTitle,
          date: new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date(best.announcementTime)),
          file: fname, bytes, url: best.pdfUrl,
          note: r.hits.length > 1 ? `命中 ${r.hits.length} 条，取最新` : '',
        });
        console.log(`  ✓ ${code} ${year} ${label}  ${best.secName}  ${(bytes / 1024 / 1024).toFixed(2)}MB  ${fname}`);
      } catch (e) {
        rows.push({ code, year, label, status: '下载失败', secName: best.secName, title: best.announcementTitle, url: best.pdfUrl, note: e.message });
        console.log(`  ! ${code} ${year} ${label}  下载失败: ${e.message}`);
      }
      await sleep(300);
    }
  }
}

const esc = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
const head = ['股票代码', '公司名称', '会计年度', '报告类型', '状态', '公告标题', '公告日期', '文件名', '大小MB', '链接', '备注'];
const lines = [head.join(',')];
for (const r of rows) {
  lines.push([r.code, r.secName ?? '', r.year, r.label, r.status, r.title ?? '', r.date ?? '',
    r.file ?? '', r.bytes ? (r.bytes / 1024 / 1024).toFixed(2) : '', r.url ?? '', r.note ?? ''].map(esc).join(','));
}
await writeFile(`${OUT}/manifest.csv`, '\ufeff' + lines.join('\n') + '\n', 'utf8');

const tally = {};
for (const r of rows) tally[r.status] = (tally[r.status] || 0) + 1;
console.log('\n状态分布:', JSON.stringify(tally).replace(/"/g, ''));
console.log('清单:', `${OUT}/manifest.csv`);
