/**
 * FinKB .env 的读写逻辑（Host 侧与测试共用）。
 *
 * 设计目标：
 * - 只改我们管理的 5 个键，其他行（注释、别的密钥）原样保留；
 * - 空字符串 = 不修改（密钥输入框留空时不应把已有 key 清掉）；
 * - 写盘用临时文件 + rename，权限 0600；
 * - 不把密钥回传给浏览器，只回“是否已配置”。
 */
import { promises as fs } from 'node:fs';
import path from 'node:path';

/** 界面字段 → .env 键。 */
export const FIELD_TO_ENV = {
  llmApiKey: 'LLM_API_KEY',
  llmBaseUrl: 'LLM_BASE_URL',
  llmModel: 'LLM_MODEL',
  siliconflowApiKey: 'SILICONFLOW_API_KEY',
  mineruToken: 'MINERU_API_TOKEN',
};

/** 只读回显的键（非密钥，可以回传）。 */
export const VISIBLE_ENV = ['LLM_BASE_URL', 'LLM_MODEL'];

/** 密钥键：只回“是否已配置”。 */
export const SECRET_ENV = ['LLM_API_KEY', 'SILICONFLOW_API_KEY', 'MINERU_API_TOKEN'];

/** 解析 .env 文本为行数组；保留注释与空行。 */
export function parseEnv(text) {
  const lines = String(text ?? '').split(/\r?\n/);
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

/** 从 .env 行里取键值（支持 export、引号）。 */
export function envMap(lines) {
  const map = new Map();
  for (const line of lines) {
    const parsed = parseLine(line);
    if (parsed) map.set(parsed.key, parsed.value);
  }
  return map;
}

function parseLine(line) {
  const trimmed = String(line).trim();
  if (trimmed === '' || trimmed.startsWith('#')) return undefined;
  const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(trimmed);
  if (!match) return undefined;
  let value = match[2].trim();
  if (
    value.length >= 2 &&
    ((value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'")))
  ) {
    value = value.slice(1, -1);
  }
  return { key: match[1], value };
}

function formatValue(value) {
  if (/^[A-Za-z0-9_./:@%+\-=]+$/.test(value)) return value;
  return '"' + value.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
}

/**
 * 合并更新：updates 的键为 .env 键；值为空/undefined 表示保留原值。
 * @returns {string} 新的 .env 文本
 */
export function mergeEnv(text, updates) {
  const lines = parseEnv(text);
  const pending = new Map();
  for (const [key, value] of Object.entries(updates ?? {})) {
    if (typeof value !== 'string' || value.trim() === '') continue;
    pending.set(key, value.trim());
  }
  if (pending.size === 0) return lines.join('\n') + (lines.length ? '\n' : '');

  const written = new Set();
  const next = lines.map((line) => {
    const parsed = parseLine(line);
    if (!parsed || !pending.has(parsed.key)) return line;
    written.add(parsed.key);
    return `${parsed.key}=${formatValue(pending.get(parsed.key))}`;
  });
  for (const [key, value] of pending) {
    if (!written.has(key)) next.push(`${key}=${formatValue(value)}`);
  }
  return next.join('\n') + '\n';
}

/** 读 .env 为 Map；不存在返回空 Map。 */
export async function readEnv(envPath) {
  try {
    return envMap(parseEnv(await fs.readFile(envPath, 'utf8')));
  } catch (error) {
    if (error && error.code === 'ENOENT') return new Map();
    throw error;
  }
}

/** 原子写 .env：临时文件 + rename，权限 0600。 */
export async function writeEnv(envPath, text) {
  const dir = path.dirname(envPath);
  await fs.mkdir(dir, { recursive: true });
  const tmp = path.join(dir, `.env.tmp-${process.pid}-${Date.now()}`);
  await fs.writeFile(tmp, text, { encoding: 'utf8', mode: 0o600 });
  await fs.chmod(tmp, 0o600).catch(() => {});
  await fs.rename(tmp, envPath);
}

/** 把开头的 `~` 展开为用户目录；其余原样。 */
export function expandHome(value, homedir = '') {
  return String(value ?? '').replace(/^~(?=$|\/|\\)/, homedir);
}

/** 解析知识库目录：显式设置 > FINKB_HOME > ~/.finkb。 */
export function resolveHome({ explicit, envHome, homedir } = {}) {
  const candidates = [explicit, envHome, path.join(homedir ?? '', '.finkb')];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim() !== '') {
      return expandHome(candidate.trim(), homedir ?? '');
    }
  }
  return path.join(homedir ?? '', '.finkb');
}

/** 生成给浏览器的状态（不回传密钥）。 */
export function publicStatus(home, envPath, map, exists) {
  const configured = {};
  for (const key of SECRET_ENV) {
    const field = Object.keys(FIELD_TO_ENV).find((name) => FIELD_TO_ENV[name] === key);
    configured[field] = map.has(key) && String(map.get(key)).trim() !== '';
  }
  const values = {};
  for (const key of VISIBLE_ENV) values[key] = map.get(key) ?? '';
  return {
    home,
    envPath,
    exists: Boolean(exists),
    configured,
    values: {
      llmBaseUrl: values.LLM_BASE_URL ?? '',
      llmModel: values.LLM_MODEL ?? '',
    },
  };
}
