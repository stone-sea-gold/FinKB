/**
 * FinKB 设置页 —— 浏览器半。
 *
 * 注册到 DSH 的 `settings.section` 槽：设置页左侧会多一个「FinKB」，
 * 卡片里直接填密钥，保存后写进 `$FINKB_HOME/config/.env`。
 *
 * 客户端契约与普通 ESM 不同：文件包在 `window.__ModuleLoader__.load({id, factory})` 里，
 * `require` 由 DSH 模块系统解析（react 由内核提供）。手写即可，不需要构建管线。
 */
window.__ModuleLoader__.load({
  id: 'finkb-dsh',
  factory: (require) => {
    const module = { exports: {} };
    const exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' });

    const React = require('react');
    const h = React.createElement;

    const inject = ['slots'];
    const API = '/finkb-settings/api';

    const T = {
      bg: 'var(--dsw-alias-bg-layer-1)',
      bgNested: 'var(--dsw-alias-bg-layer-2)',
      border: 'var(--dsw-alias-border-l1)',
      label: 'var(--dsw-alias-label-primary)',
      label2: 'var(--dsw-alias-label-secondary)',
      ok: 'var(--dsw-alias-state-success-primary)',
      err: 'var(--dsw-alias-state-error-primary)',
      accent: 'var(--dsw-alias-brand-primary, var(--dsw-alias-label-primary))',
    };

    const FIELDS = [
      {
        key: 'finkbHome',
        label: '知识库目录',
        hint: 'FinKB 数据目录。留空则用 FINKB_HOME，再退到 ~/.finkb。源码仓库模式通常填 .../reporting/finkb。',
        placeholder: '例如 /home/me/code/reporting/finkb',
      },
      {
        key: 'llmApiKey',
        label: 'LLM API Key',
        secret: true,
        hint: '事实抽取、合成、视觉解读都用它。OpenCode / OpenAI 兼容服务均可。',
      },
      {
        key: 'llmBaseUrl',
        label: 'LLM Base URL',
        hint: '留空使用 FinKB 默认值（https://opencode.ai/zen/go）。',
      },
      {
        key: 'llmModel',
        label: 'LLM 模型',
        hint: '留空使用默认 deepseek-v4.1-flash；支持识图的模型才能跑视觉解读。',
      },
      {
        key: 'siliconflowApiKey',
        label: 'SiliconFlow API Key（可选）',
        secret: true,
        hint: '只用于向量检索 bge-m3；不填则检索退化为关键词通道。',
      },
      {
        key: 'mineruToken',
        label: 'MinerU Token（可选）',
        secret: true,
        hint: '只用于导入新 PDF 时的高质量表格解析；不填则用离线解析。',
      },
    ];

    async function call(body) {
      const response = await fetch(API, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok !== true) {
        throw new Error(data.message || `HTTP ${response.status}`);
      }
      return data;
    }

    const inputStyle = {
      width: '100%',
      boxSizing: 'border-box',
      height: '32px',
      padding: '0 10px',
      borderRadius: '8px',
      border: '0.5px solid ' + T.border,
      background: T.bgNested,
      color: T.label,
      fontSize: '13px',
      outline: 'none',
    };

    function Field({ field, value, placeholder, onChange }) {
      return h(
        'label',
        {
          key: field.key,
          style: { display: 'flex', flexDirection: 'column', gap: '4px', minWidth: 0 },
        },
        [
          h('span', { key: 'l', style: { fontSize: '13px', fontWeight: 600, color: T.label } }, field.label),
          h('input', {
            key: 'i',
            type: field.secret ? 'password' : 'text',
            autoComplete: 'off',
            spellCheck: false,
            value,
            placeholder,
            onChange: (event) => onChange(field.key, event.target.value),
            style: inputStyle,
          }),
          field.hint
            ? h('span', { key: 'h', style: { fontSize: '12px', color: T.label2, lineHeight: 1.5 } }, field.hint)
            : null,
        ],
      );
    }

    function FinKbSettings() {
      const [status, setStatus] = React.useState(null);
      const [values, setValues] = React.useState({});
      const [secrets, setSecrets] = React.useState({});
      const [busy, setBusy] = React.useState(false);
      const [message, setMessage] = React.useState('');

      React.useEffect(() => {
        let alive = true;
        call({ action: 'status' })
          .then((data) => {
            if (!alive) return;
            setStatus(data);
            setValues({ finkbHome: data.home, llmBaseUrl: data.values.llmBaseUrl, llmModel: data.values.llmModel });
          })
          .catch((error) => {
            if (alive) setMessage('读取状态失败：' + error.message);
          });
        return () => {
          alive = false;
        };
      }, []);

      const update = (key, value) => {
        if (FIELDS.find((field) => field.key === key)?.secret) {
          setSecrets((previous) => ({ ...previous, [key]: value }));
        } else {
          setValues((previous) => ({ ...previous, [key]: value }));
        }
      };

      const save = async () => {
        setBusy(true);
        setMessage('');
        try {
          const data = await call({ action: 'save', finkbHome: values.finkbHome ?? '', values: secrets });
          setStatus(data);
          setSecrets({});
          setValues((previous) => ({
            ...previous,
            finkbHome: data.home,
            llmBaseUrl: data.values.llmBaseUrl,
            llmModel: data.values.llmModel,
          }));
          setMessage('已保存，下一次工具调用生效（无需重启）。');
        } catch (error) {
          setMessage('保存失败：' + error.message);
        } finally {
          setBusy(false);
        }
      };

      const configured = status?.configured ?? {};
      const placeholderFor = (field) => {
        if (!field.secret) return field.placeholder ?? '';
        return configured[field.key] ? '已配置（留空保持不变）' : '未配置';
      };
      const valueFor = (field) => (field.secret ? secrets[field.key] ?? '' : values[field.key] ?? '');

      return h(
        'div',
        {
          style: {
            padding: '16px',
            maxWidth: '680px',
            width: '100%',
            minWidth: 0,
            boxSizing: 'border-box',
            color: T.label,
            display: 'flex',
            flexDirection: 'column',
            gap: '12px',
          },
        },
        [
          h('div', { key: 'title', style: { fontSize: '16px', fontWeight: 700 } }, 'FinKB 知识库与模型'),
          h(
            'p',
            { key: 'intro', style: { margin: 0, color: T.label2, fontSize: '13px', lineHeight: 1.6 } },
            '这里的配置写入 FinKB 的 .env 文件。保存后立即生效：FinKB 每次调用都会重新读取 .env，不需要重启 DSH 或 MCP 进程。',
          ),
          status
            ? h(
                'div',
                {
                  key: 'status',
                  style: {
                    padding: '10px 12px',
                    borderRadius: '10px',
                    border: '0.5px solid ' + T.border,
                    background: T.bg,
                    fontSize: '12px',
                    color: T.label2,
                    lineHeight: 1.7,
                    wordBreak: 'break-all',
                  },
                },
                [
                  h('div', { key: 'p' }, '配置文件：' + status.envPath + (status.exists ? '' : '（尚不存在，保存时创建）')),
                  h(
                    'div',
                    { key: 'c' },
                    '已配置：' +
                      [
                        configured.llmApiKey ? 'LLM Key' : null,
                        configured.siliconflowApiKey ? 'SiliconFlow Key' : null,
                        configured.mineruToken ? 'MinerU Token' : null,
                      ]
                        .filter(Boolean)
                        .join('、') || '无',
                  ),
                ],
              )
            : null,
          ...FIELDS.map((field) =>
            Field({
              field,
              value: valueFor(field),
              placeholder: placeholderFor(field),
              onChange: update,
            }),
          ),
          h(
            'div',
            { key: 'actions', style: { display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' } },
            [
              h(
                'button',
                {
                  key: 'save',
                  type: 'button',
                  disabled: busy,
                  onClick: save,
                  style: {
                    height: '32px',
                    padding: '0 16px',
                    borderRadius: '8px',
                    border: '0.5px solid ' + T.border,
                    background: T.accent,
                    color: '#fff',
                    fontSize: '13px',
                    fontWeight: 600,
                    cursor: busy ? 'default' : 'pointer',
                    opacity: busy ? 0.6 : 1,
                  },
                },
                busy ? '保存中…' : '保存',
              ),
              message
                ? h(
                    'span',
                    {
                      key: 'msg',
                      role: 'status',
                      style: {
                        fontSize: '12px',
                        color: message.startsWith('已保存') ? T.ok : T.err,
                        lineHeight: 1.5,
                      },
                    },
                    message,
                  )
                : null,
            ],
          ),
          h(
            'p',
            { key: 'note', style: { margin: 0, color: T.label2, fontSize: '12px', lineHeight: 1.6 } },
            '提示：DSH 启动 MCP 子进程时会清洗名字含 KEY/TOKEN/SECRET 的环境变量，所以密钥请填在这里（或 $FINKB_HOME/config/.env），不要只放在 shell 里。',
          ),
        ],
      );
    }

    function apply(ctx) {
      ctx.slots.inject('settings.section', () =>
        ctx.slots.register(
          { name: 'settings.section', id: 'finkb', label: 'FinKB', order: 21 },
          FinKbSettings,
        ),
      );
    }

    exports.apply = apply;
    exports.inject = inject;
    exports.__view = FinKbSettings;
    return module.exports;
  },
});
