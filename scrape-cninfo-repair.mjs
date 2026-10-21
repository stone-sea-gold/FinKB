#!/usr/bin/env node
/**
 * 定向补齐 scrape-cninfo.mjs 漏掉的 3 份报告。
 * 已定位到两处判定缺陷（重试无法解决）：
 *  1. 920179 凯德石英由北交所旧代码 835179 变更而来，2025 年上半年的公告仍挂在
 *     旧代码下，需要放宽 secCode 匹配。
 *  2. 600301 华锡有色 2026 一季报标题为《…第一季度报告》，标题内不含年份，
 *     严格判定永远不命中，需要在窄时间窗下放宽年份要求。
 */
import { readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';

const MOD = '/home/generalblack/.npm/_npx/b5abb3253bb86cf1/node_modules/@duhu2000/cninfo-mcp/lib/cninfo.mjs';
if (!existsSync(MOD)) { console.error('找不到 cninfo-mcp 模块'); process.exit(1); }
const { internals, isReportTitle, resolveAttachmentUrl } = await import(MOD);

const OUT_DIR = '/home/generalblack/code/works/reporting/raw';
const QUERY_URL = 'https://www.cninfo.com.cn/new/hisAnnouncement/query';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36';
const HEADERS = {
  accept: 'application/json, text/javascript, */*; q=0.01',
  'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
  origin: 'https://www.cninfo.com.cn',
  referer: 'https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice',
  'x-requested-with': 'XMLHttpRequest', 'user-agent': UA,
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const EXCLUDED = ['摘要', '确认意见', '取消', '更正', '补充', '说明', '提示', '致歉', '修订', '英文'];
const PHRASES = {
  annual: ['年度报告', '年报'], semiannual: ['半年度报告', '中期报告'],
  q1: ['第一季度报告', '一季度报告'], q3: ['第三季度报告', '三季度报告'],
};

/** 放宽版判定：窄时间窗下只校验类型词，不要求标题带年份 */
function relaxedTitle(title, type) {
  const c = String(title ?? '').replace(/\s+/g, '');
  if (EXCLUDED.some((w) => c.includes(w))) return false;
  return new RegExp(`^.*(?:${PHRASES[type].join('|')})$`).test(c);
}

const FIXES = [
  { code: '920179', year: 2025, type: 'semiannual', label: '半年报', secName: '凯德石英',
    column: 'bj', plate: 'bj', stock: '920179,gfbj0835179',
    seDate: '2025-01-01~2025-12-31', extraCodes: ['835179'], relaxed: false },
  { code: '920179', year: 2025, type: 'q1', label: '一季报', secName: '凯德石英',
    column: 'bj', plate: 'bj', stock: '920179,gfbj0835179',
    seDate: '2025-01-01~2025-12-31', extraCodes: ['835179'], relaxed: false },
  { code: '600301', year: 2026, type: 'q1', label: '一季报', secName: '华锡有色',
    column: 'sse', plate: 'sh', stock: '',
    seDate: '2026-03-01~2026-05-31', extraCodes: [], relaxed: true },
];

const csvEsc = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
function parseCsv(text) {
  const rows = []; let row = []; let field = ''; let inQ = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQ) {
      if (ch === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else inQ = false; }
      else field += ch;
    } else if (ch === '"') inQ = true;
    else if (ch === ',') { row.push(field); field = ''; }
    else if (ch === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
    else if (ch !== '\r') field += ch;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  return rows;
}

const newRows = [];
for (const f of FIXES) {
  let found = null;
  for (let attempt = 1; attempt <= 6 && !found; attempt++) {
    try {
      const p = internals.reportQuery(1, f.code, f.type, f.column, f.plate, f.stock);
      p.seDate = f.seDate;
      const res = await fetch(QUERY_URL, {
        method: 'POST', headers: HEADERS,
        body: new URLSearchParams(Object.entries(p).map(([k, v]) => [k, String(v ?? '')])),
        signal: AbortSignal.timeout(30_000),
      });
      const json = await res.json();
      const list = Array.isArray(json.announcements) ? json.announcements : [];
      const allowed = new Set([f.code, ...f.extraCodes]);
      const hits = list.filter((x) => {
        if (!allowed.has(String(x.secCode))) return false;
        return f.relaxed ? relaxedTitle(x.announcementTitle, f.type) : isReportTitle(x.announcementTitle, f.type, f.year);
      });
      if (hits.length) {
        hits.sort((a, b) => (b.announcementTime || 0) - (a.announcementTime || 0));
        found = { best: hits[0], n: hits.length, attempt };
      }
    } catch { /* 抖动，重试 */ }
    if (!found) await sleep(500 + Math.random() * 500);
  }

  if (!found) {
    console.log(`✗ ${f.code} ${f.year} ${f.label} 仍未命中`);
    newRows.push([f.code, f.secName, String(f.year), f.label, '未获取', '', '', '', '', '', '定向补齐 6 次仍未命中']);
    continue;
  }

  const url = resolveAttachmentUrl(found.best.adjunctUrl);
  const fname = `${f.code}_${f.year}_${f.label}.pdf`;
  const res = await fetch(url, {
    headers: { 'user-agent': UA, referer: 'https://www.cninfo.com.cn/', accept: 'application/pdf,*/*' },
    signal: AbortSignal.timeout(60_000),
  });
  const buf = Buffer.from(await res.arrayBuffer());
  if (buf.subarray(0, 5).toString('latin1') !== '%PDF-') throw new Error('非法 PDF');
  await writeFile(`${OUT_DIR}/${fname}`, buf);
  const note = f.code === '600301' ? '标题不含年份，已用窄窗(2026-03~05)定向匹配'
    : '公告挂在北交所旧代码 835179 下';
  newRows.push([f.code, found.best.secName || f.secName, String(f.year), f.label, '已下载',
    found.best.announcementTitle, new Date(found.best.announcementTime).toISOString().slice(0, 10),
    fname, (buf.length / 1024).toFixed(0), url, `${note}；第 ${found.attempt} 次尝试命中`]);
  console.log(`✓ ${f.code} ${f.year} ${f.label}  ${found.best.announcementTitle}  ${(buf.length / 1024).toFixed(0)}KB`);
  await sleep(200);
}

// ── 回写 manifest.csv ──
const manifestPath = `${OUT_DIR}/manifest.csv`;
const rows = parseCsv((await readFile(manifestPath, 'utf8')).replace(/^\ufeff/, ''));
const header = rows[0];
const body = rows.slice(1).filter((r) => r.length > 1);
let replaced = 0;
for (const nr of newRows) {
  const idx = body.findIndex((r) => r[0] === nr[0] && r[2] === nr[2] && r[3] === nr[3]);
  if (idx >= 0) { body[idx] = nr; replaced++; } else body.push(nr);
}
await writeFile(manifestPath, '\ufeff' + [header, ...body].map((r) => r.map(csvEsc).join(',')).join('\n') + '\n', 'utf8');
console.log(`\nmanifest 更新：替换 ${replaced} 行，当前共 ${body.length} 行`);
