#!/usr/bin/env node
/**
 * 抓取巨潮资讯（CNINFO）定期报告 PDF，存档到工作区 raw/ 目录。
 *
 * 关键事实（已实测验证）：
 *  1. CNINFO 的 seDate 按【公告日期】过滤，不是会计年度。年报在次年 4 月公告，
 *     因此会计年度 Y 的检索窗口必须用 [Y-01-01, 今天]。
 *  2. CNINFO 搜索接口存在高频「HTTP 200 但返回空集」的抖动（实测 25%~60%），
 *     故每次查询做有限次重试（最多 MAX_ATTEMPTS 次），拿到非空即停。
 *  3. 上游模块 @duhu2000/cninfo-mcp 的 queryReports() 硬编码 2001~今天 的全量
 *     窗口，结果按时间倒序会翻页丢失目标年份，故此处改用其 internals.reportQuery
 *     自行收窄窗口，并复用其 isReportTitle 做标题判定。
 */
import { mkdir, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';

const MOD = '/home/generalblack/.npm/_npx/b5abb3253bb86cf1/node_modules/@duhu2000/cninfo-mcp/lib/cninfo.mjs';
if (!existsSync(MOD)) {
  console.error('找不到 cninfo-mcp 模块：' + MOD);
  process.exit(1);
}
const { internals, isReportTitle, resolveAttachmentUrl } = await import(MOD);

const OUT_DIR = '/home/generalblack/code/works/reporting/raw';
const CODES = ['002023', '600961', '002428', '920179', '002725', '000060',
  '002902', '000751', '600206', '000960', '600301', '000988', '600703'];
const YEARS = [2025, 2026];
const TYPES = [['annual', '年报'], ['semiannual', '半年报'], ['q1', '一季报'], ['q3', '三季报']];

const TODAY = new Date().toISOString().slice(0, 10);
const MAX_ATTEMPTS = 5;
const MIN_GAP_MS = 180;

const QUERY_URL = 'https://www.cninfo.com.cn/new/hisAnnouncement/query';
const TOPSearch_URL = 'https://www.cninfo.com.cn/new/information/topSearch/query';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36';
const HEADERS = {
  accept: 'application/json, text/javascript, */*; q=0.01',
  'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
  origin: 'https://www.cninfo.com.cn',
  referer: 'https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice',
  'x-requested-with': 'XMLHttpRequest',
  'user-agent': UA,
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const norm = (s) => String(s ?? '').replace(/\s+/g, '');

/** 按代码前缀判定交易所分支 */
function branchOf(code) {
  if (/^6/.test(code)) return { column: 'sse', plate: 'sh', stock: '', label: '沪市' };
  if (/^(0|3)/.test(code)) return { column: 'szse', plate: 'sz', stock: '', label: '深市' };
  return { column: 'bj', plate: 'bj', stock: null, label: '北交所' }; // stock 需 orgId
}

async function postForm(url, data) {
  const res = await fetch(url, {
    method: 'POST',
    headers: HEADERS,
    body: new URLSearchParams(Object.entries(data).map(([k, v]) => [k, String(v ?? '')])),
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

/** 北交所需要先解析 orgId */
async function resolveOrgId(code) {
  const hits = await postForm(TOPSearch_URL, { keyWord: code, maxNum: 10 });
  if (!Array.isArray(hits)) throw new Error('orgId 响应异常');
  const item = hits.find((e) => String(e.code) === code && e.orgId) || hits.find((e) => e.orgId);
  return item ? { code: String(item.code), orgId: String(item.orgId) } : null;
}

/** 单次查询：返回该窗口内所有公告 */
async function queryOnce(code, type, year, branch) {
  const payload = internals.reportQuery(1, code, type, branch.column, branch.plate, branch.stock ?? '');
  payload.seDate = `${year}-01-01~${TODAY}`;
  const json = await postForm(QUERY_URL, payload);
  if (!json || typeof json !== 'object' || !('announcements' in json)) throw new Error('响应结构异常');
  const list = Array.isArray(json.announcements) ? json.announcements : [];
  const hits = list
    .filter((x) => String(x.secCode) === code && isReportTitle(x.announcementTitle, type, year))
    .map((x) => {
      // CNINFO 返回的 adjunctUrl 是相对路径（如 finalpage/2026-04-28/xxx.PDF），
      // 必须解析为 https://static.cninfo.com.cn/... 才能下载
      let pdfUrl = '';
      try { pdfUrl = resolveAttachmentUrl(x.adjunctUrl); } catch { /* 非法链接，丢弃 */ }
      return { ...x, pdfUrl };
    })
    .filter((x) => x.pdfUrl);
  hits.sort((a, b) => (b.announcementTime || 0) - (a.announcementTime || 0));
  return { total: json.totalRecordNum ?? 0, rawLen: list.length, hits };
}

/** 有限次重试直到命中；返回 {hits, attempts} */
async function queryWithRetry(code, type, year, branch, maxAttempts = MAX_ATTEMPTS) {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      const r = await queryOnce(code, type, year, branch);
      if (r.hits.length > 0) return { ...r, attempts: attempt };
      if (attempt === maxAttempts) return { ...r, attempts: attempt };
    } catch (e) {
      if (attempt === maxAttempts) return { hits: [], attempts: attempt, error: e.message };
    }
    await sleep(400 + Math.random() * 500);
  }
  return { hits: [], attempts: maxAttempts };
}

async function download(url, destPath) {
  const res = await fetch(url, {
    headers: { 'user-agent': UA, referer: 'https://www.cninfo.com.cn/', accept: 'application/pdf,*/*' },
    signal: AbortSignal.timeout(60_000),
  });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const buf = Buffer.from(await res.arrayBuffer());
  if (buf.length < 5 || buf.subarray(0, 5).toString('latin1') !== '%PDF-') {
    throw new Error('不是有效 PDF（' + buf.length + ' bytes）');
  }
  await writeFile(destPath, buf);
  return buf.length;
}

// ─────────────────────────── 主流程 ───────────────────────────
await mkdir(OUT_DIR, { recursive: true });
const rows = [];
const orgIdCache = new Map();
let reqCount = 0;

const origFetch = globalThis.fetch;
globalThis.fetch = (...a) => { reqCount++; return origFetch(...a); };

console.log(`CNINFO 抓取开始 窗口=[FY-01-01 ~ ${TODAY}] 重试上限=${MAX_ATTEMPTS}`);
console.log(`代码 ${CODES.length} 只 × 年度 ${YEARS.join('/')} × 类型 年报/半年报/一季报/三季报\n`);

for (const code of CODES) {
  let branch = branchOf(code);
  if (branch.stock === null) {
    try {
      if (!orgIdCache.has(code)) orgIdCache.set(code, await resolveOrgId(code));
      const org = orgIdCache.get(code);
      if (!org) throw new Error('无法解析 orgId');
      branch = { ...branch, stock: `${org.code},${org.orgId}` };
    } catch (e) {
      console.log(`!! ${code} 北交所 orgId 解析失败: ${e.message}`);
      for (const year of YEARS) for (const [type, label] of TYPES) {
        rows.push({ code, year, type, label, status: '失败', note: '不可达: ' + e.message });
      }
      continue;
    }
  }

  for (const year of YEARS) {
    for (const [type, label] of TYPES) {
      // FY2026 年报在 2027-04-30 前才披露，当前(2026-10-16)必然尚未发布，不查询
      if (year === 2026 && type === 'annual') {
        rows.push({ code, year, type, label, status: '尚未发布', note: 'FY2026 年报披露期未到' });
        continue;
      }

      // FY2026 三季报法定期限 2026-10-31，今日(2026-10-16)多数尚未披露，少retry 观察
      const budget = (year === 2026 && type === 'q3') ? 3 : MAX_ATTEMPTS;
      const r = await queryWithRetry(code, type, year, branch, budget);
      await sleep(MIN_GAP_MS);

      if (r.hits.length === 0) {
        rows.push({
          code, year, type, label,
          status: (year === 2026 && type === 'q3') ? '尚未披露' : '未获取',
          note: r.error ? '请求异常: ' + r.error
            : (year === 2026 && type === 'q3') ? `法定披露截止 2026-10-31，查询时点尚未发布（${r.attempts} 次尝试）`
              : `重试 ${r.attempts} 次均返回空集`,
        });
        console.log(`  ✗ ${code} ${year} ${label}  ${(year === 2026 && type === 'q3') ? '尚未披露' : '未获取'} (${r.attempts} 次尝试)`);
        continue;
      }

      const best = r.hits[0];
      const name = best.secName || '';
      const fname = `${code}_${year}_${label}.pdf`;
      const fpath = `${OUT_DIR}/${fname}`;
      try {
        const bytes = await download(best.pdfUrl, fpath);
        const note = r.hits.length > 1 ? `窗口内命中 ${r.hits.length} 条，取最新` : '';
        rows.push({
          code, year, type, label, status: '已下载', secName: name,
          title: best.announcementTitle,
          date: new Date(best.announcementTime).toISOString().slice(0, 10),
          file: fname, bytes, url: best.pdfUrl, note,
        });
        console.log(`  ✓ ${code} ${year} ${label}  ${name}  ${(bytes / 1024).toFixed(0)}KB  ${fname}`);
      } catch (e) {
        rows.push({
          code, year, type, label, status: '下载失败', secName: name,
          title: best.announcementTitle, url: best.pdfUrl, note: e.message,
        });
        console.log(`  ! ${code} ${year} ${label}  下载失败: ${e.message}`);
      }
      await sleep(MIN_GAP_MS);
    }
  }
}

// ─────────────────────────── 清单 ───────────────────────────
const csvEsc = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
const header = ['股票代码', '公司名称', '会计年度', '报告类型', '状态', '公告标题', '公告日期', '文件名', '大小KB', '链接', '备注'];
const lines = [header.join(',')];
for (const r of rows) {
  lines.push([
    r.code, r.secName ?? '', r.year, r.label, r.status, r.title ?? '', r.date ?? '',
    r.file ?? '', r.bytes ? (r.bytes / 1024).toFixed(0) : '', r.url ?? '', r.note ?? '',
  ].map(csvEsc).join(','));
}
await writeFile(`${OUT_DIR}/manifest.csv`, '\ufeff' + lines.join('\n') + '\n', 'utf8');

const tally = {};
for (const r of rows) tally[r.status] = (tally[r.status] || 0) + 1;
console.log('\n════════ 汇总 ════════');
console.log('上游请求数:', reqCount);
console.log('状态分布:', JSON.stringify(tally, null, 0).replace(/"/g, ''));
console.log('清单:', `${OUT_DIR}/manifest.csv`);
for (const s of ['未获取', '下载失败', '失败']) {
  const bad = rows.filter((r) => r.status === s);
  if (bad.length) console.log(`\n[${s}] ${bad.length} 项:`);
  for (const r of bad) console.log(`   ${r.code} (${r.secName || '?'}) ${r.year} ${r.label} — ${r.note}`);
}
