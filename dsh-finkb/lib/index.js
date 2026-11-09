/**
 * FinKB 设置页 —— Host 半。
 *
 * 只做两件事：
 * 1. 在 DSH 的 Web 服务上挂一个同源接口 `/finkb-settings/api`（读状态 / 写 .env）；
 * 2. 把界面提交的密钥写进 `$FINKB_HOME/config/.env`。
 *
 * 为什么写完不用重启：FinKB 每次调用 LLM/解析时都会重新读 `.env`
 * （`LLMConfig.from_env()` 内部 `load_env_file()`），所以保存后下一次工具调用就生效。
 */
import { promises as fs } from 'node:fs';
import { spawn } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import z from '@deepseek-ai/schemastery';
import {
  FIELD_TO_ENV,
  mergeEnv,
  publicStatus,
  readEnv,
  resolveHome,
  writeEnv,
} from './env.js';

export const name = 'finkb-settings';

/** webServer 是可选服务：纯对话部署没有它时，插件仍可加载，只是没有设置接口。 */
export const inject = [];

export const Config = z.object({
  // 知识库目录是部署期配置：优先 FINKB_HOME，其次这里配置的 finkbHome，
  // 最后退到 ~/.finkb。卡片只负责写 .env，改 Key 能热加载，改目录不能
  // （MCP 子进程启动时已固定），所以这个字段只允许从 profile patch 设置。
  finkbHome: z.string().default(''),
  // 导入年报时调用的 Python 解释器（需装好 finkb 依赖）。留空依次尝试
  // FINKB_PYTHON 环境变量、python3；桌面启动的 DSH 建议在 profile patch 里
  // 直接写 .venv/bin/python 的绝对路径。
  pythonBin: z.string().default(''),
});

const REPORT_TYPES = ['年报', '半年报', '一季报', '三季报'];
const PERIOD_RE = /^20\d{2}(?:H1|Q1|Q3)?$/;
const COMPANY_RE = /^\d{6}$/;

/** 同一时间只允许一个导入任务；任务状态只保存在内存里，进程重启即丢。 */
const importJobs = new Map();

function pythonFor(configPython) {
  return configPython || process.env.FINKB_PYTHON || 'python3';
}

const MAX_BODY_BYTES = 64 * 1024;

/** 知识库目录只由部署环境决定：finkbHome 配置 > FINKB_HOME > ~/.finkb。 */
function currentHome(explicit = '') {
  return path.resolve(
    resolveHome({
      explicit,
      envHome: process.env.FINKB_HOME || '',
      homedir: os.homedir(),
    }),
  );
}

async function envPathFor(home) {
  return path.join(home, 'config', '.env');
}

async function readEnvText(envPath) {
  try {
    return await fs.readFile(envPath, 'utf8');
  } catch (error) {
    if (error && error.code === 'ENOENT') return '';
    throw error;
  }
}

async function statusFor(explicitHome = '') {
  const home = currentHome(explicitHome);
  const envPath = await envPathFor(home);
  const map = await readEnv(envPath);
  const exists = await fs
    .access(envPath)
    .then(() => true)
    .catch(() => false);
  return { ...publicStatus(home, envPath, map, exists), rawDir: path.join(home, 'raw') };
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error('请求体过大'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try {
        const raw = Buffer.concat(chunks).toString('utf8');
        resolve(raw === '' ? {} : JSON.parse(raw));
      } catch {
        reject(new Error('请求体不是合法 JSON'));
      }
    });
    req.on('error', reject);
  });
}

function writeJson(res, statusCode, value) {
  const body = JSON.stringify(value);
  res.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
  });
  res.end(body);
}

function parseAuthority(value) {
  try {
    return new URL('http://' + value);
  } catch {
    return undefined;
  }
}

function isLoopbackHostname(hostname) {
  const name = String(hostname).toLowerCase().replace(/^\[|\]$/g, '');
  return name === 'localhost' || name === '127.0.0.1' || name === '::1';
}

/** 同源 + 本地/受信 Host：默认只允许本机页面调用这个接口。 */
function trustedRequest(req, trustedHosts = []) {
  const host = req.headers?.host;
  if (!host) return false;
  const hostUrl = parseAuthority(host);
  if (!hostUrl) return false;
  const allowed = isLoopbackHostname(hostUrl.hostname)
    ? true
    : trustedHosts.some((entry) => {
        const value = typeof entry === 'string' ? entry : entry?.host;
        return typeof value === 'string' && value === hostUrl.host;
      });
  if (!allowed) return false;
  if (req.headers['sec-fetch-site'] === 'cross-site') return false;
  const origin = req.headers.origin;
  if (!origin) return true;
  try {
    return new URL(origin).host === hostUrl.host;
  } catch {
    return false;
  }
}

async function startImport(body, home, pythonBin) {
  const running = [...importJobs.values()].find((job) => job.state === 'running');
  if (running) return { error: '已有一个导入任务在运行，请等它结束' };

  const pdfPath = String(body?.pdfPath ?? '').trim();
  const company = String(body?.company ?? '').trim();
  const period = String(body?.period ?? '').trim();
  const reportType = String(body?.reportType ?? '').trim();

  if (!pdfPath) return { error: '请填写本机 PDF 的绝对路径' };
  if (!COMPANY_RE.test(company)) return { error: '公司代码必须是 6 位数字' };
  if (!PERIOD_RE.test(period)) return { error: '期间格式应为 2025 / 2025H1 / 2025Q1 / 2025Q3' };
  if (!REPORT_TYPES.includes(reportType)) return { error: '报告类型必须是 年报/半年报/一季报/三季报' };

  try {
    const stat = await fs.stat(pdfPath);
    if (!stat.isFile()) return { error: '路径不是文件：' + pdfPath };
  } catch {
    return { error: '找不到 PDF 文件：' + pdfPath };
  }

  const envPath = await envPathFor(home);
  const envMap = await readEnv(envPath);
  const args = [
    '-m', 'finkb.library', 'add',
    '--pdf', pdfPath,
    '--company', company,
    '--period', period,
    '--type', reportType,
  ];
  if (envMap.SILICONFLOW_API_KEY) args.push('--embed');

  const id = 'imp_' + Date.now().toString(36);
  const job = {
    id,
    state: 'running',
    lines: [],
    exitCode: null,
    startedAt: new Date().toISOString(),
    pdfPath,
    company,
    period,
    reportType,
  };
  importJobs.set(id, job);

  const push = (text) => {
    for (const line of String(text).split('\n')) {
      if (line.trim()) job.lines.push(line.slice(0, 500));
    }
    if (job.lines.length > 400) job.lines.splice(0, job.lines.length - 400);
  };

  try {
    const child = spawn(pythonBin, args, {
      cwd: home,
      env: { ...process.env, FINKB_HOME: home, PYTHONUNBUFFERED: '1' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    child.stdout?.setEncoding('utf8');
    child.stderr?.setEncoding('utf8');
    child.stdout?.on('data', push);
    child.stderr?.on('data', push);
    child.on('error', (error) => {
      job.state = 'failed';
      job.exitCode = -1;
      push('启动 Python 失败：' + error.message);
    });
    child.on('close', (code) => {
      if (job.state === 'running') {
        job.state = code === 0 ? 'done' : 'failed';
        job.exitCode = code;
      }
    });
  } catch (error) {
    job.state = 'failed';
    job.exitCode = -1;
    push('启动导入失败：' + error.message);
  }

  return { jobId: id, state: job.state };
}

async function handleApi(req, res, logger, trustedHosts, explicitHome = '', pythonBin = '') {
  if (!trustedRequest(req, trustedHosts)) {
    writeJson(res, 403, { ok: false, message: 'forbidden' });
    return;
  }
  if (req.method !== 'POST') {
    res.writeHead(405, { allow: 'POST' });
    res.end();
    return;
  }

  try {
    const body = await readBody(req);
    const action = body?.action;

    if (action === 'import') {
      const started = await startImport(body, currentHome(explicitHome), pythonFor(pythonBin));
      if (started.error) {
        writeJson(res, 400, { ok: false, message: started.error });
        return;
      }
      logger?.info?.(`导入任务已启动：${started.jobId}`);
      writeJson(res, 200, { ok: true, ...started });
      return;
    }

    if (action === 'importStatus') {
      const job = importJobs.get(String(body?.jobId ?? ''));
      if (!job) {
        writeJson(res, 404, { ok: false, message: '导入任务不存在（DSH 重启后任务记录会清空）' });
        return;
      }
      writeJson(res, 200, {
        ok: true,
        jobId: job.id,
        state: job.state,
        exitCode: job.exitCode,
        lines: job.lines.slice(-120),
      });
      return;
    }

    if (action === 'status') {
      writeJson(res, 200, { ok: true, ...(await statusFor(explicitHome)) });
      return;
    }

    if (action === 'save') {
      const home = currentHome(explicitHome);
      const envPath = await envPathFor(home);
      const previous = await readEnvText(envPath);
      const updates = {};
      for (const [field, envKey] of Object.entries(FIELD_TO_ENV)) {
        const value = body?.values?.[field];
        if (typeof value === 'string') updates[envKey] = value;
      }
      const next = mergeEnv(previous, updates);
      await writeEnv(envPath, next);
      const map = await readEnv(envPath);
      logger?.info?.(`settings saved to ${envPath}`);
      writeJson(res, 200, { ok: true, ...publicStatus(home, envPath, map, true) });
      return;
    }

    writeJson(res, 400, { ok: false, message: `未知 action：${String(action)}` });
  } catch (error) {
    logger?.warn?.(`settings api error: ${error?.message ?? error}`);
    writeJson(res, 400, { ok: false, message: error instanceof Error ? error.message : String(error) });
  }
}

function mountApi(wctx, logger, trustedHosts, explicitHome, pythonBin) {
  return wctx.webServer.register({
    kind: 'prefix',
    path: '/finkb-settings/api',
    handler: (req, res) => handleApi(req, res, logger, trustedHosts, explicitHome, pythonBin),
  });
}

export function apply(ctx, config) {
  const logger = ctx.logger('finkb-settings');
  const explicitHome = String(config?.finkbHome ?? '');
  const pythonBin = String(config?.pythonBin ?? '');
  // webStartup 是 DSH 0.2.x 新服务，webRuntime 是旧名；两者取其一，避免重复挂载。
  const candidates = new Map();
  let active;
  let dispose;

  const reconcile = () => {
    const next = candidates.get('webStartup') ?? candidates.get('webRuntime');
    if (next === active) return;
    dispose?.();
    dispose = undefined;
    active = next;
    if (next) {
      dispose = mountApi(next.ctx, logger, next.trustedHosts, explicitHome, pythonBin);
      logger.info?.('FinKB 设置接口已挂载：/finkb-settings/api');
    }
  };

  for (const service of ['webStartup', 'webRuntime']) {
    ctx.inject(['webServer', service], (wctx) => {
      wctx.effect(() => {
        const candidate = { ctx: wctx, trustedHosts: wctx[service]?.trustedHosts ?? [] };
        candidates.set(service, candidate);
        reconcile();
        return () => {
          if (candidates.get(service) === candidate) candidates.delete(service);
          reconcile();
        };
      });
    });
  }
}
