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
      warn: 'var(--dsw-alias-state-warning-primary, #b45309)',
      accent: 'var(--dsw-alias-brand-primary, var(--dsw-alias-label-primary))',
    };

    const FIELDS = [
      {
        key: 'llmApiKey',
        label: 'LLM API Key（必填）',
        secret: true,
        hint: '事实抽取、合成、视觉解读都依赖它；不填则指标/本体/合成不可用。OpenCode / OpenAI 兼容服务均可。',
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
        label: 'SiliconFlow API Key（建议填写）',
        secret: true,
        hint: '用于 bge-m3 向量检索；不填则检索退化为纯关键词，同义问法与语义召回会明显变差（免费额度即可）。',
      },
      {
        key: 'mineruToken',
        label: 'MinerU Token（建议填写）',
        secret: true,
        hint: '用于导入年报时的高质量表格解析（扫描页、多层表头、跨页表）；不填改用离线解析，表格结构与扫描页质量会明显下降。',
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
      const [importForm, setImportForm] = React.useState({
        pdfPath: '',
        company: '',
        period: '2025',
        reportType: '年报',
      });
      const [importJob, setImportJob] = React.useState(null);
      const [importBusy, setImportBusy] = React.useState(false);
      const [importMessage, setImportMessage] = React.useState('');
      const [wsChoice, setWsChoice] = React.useState('');
      const [wsBusy, setWsBusy] = React.useState(false);
      const [wsMessage, setWsMessage] = React.useState('');

      React.useEffect(() => {
        let alive = true;
        call({ action: 'status' })
          .then((data) => {
            if (!alive) return;
            setStatus(data);
            setWsChoice(data.selectedWorkspacePath || '');
            setValues({ llmBaseUrl: data.values.llmBaseUrl, llmModel: data.values.llmModel });
          })
          .catch((error) => {
            if (alive) setMessage('读取状态失败：' + error.message);
          });
        return () => {
          alive = false;
        };
      }, []);

      React.useEffect(() => {
        if (!importJob || importJob.state !== 'running') return undefined;
        const timer = setInterval(async () => {
          try {
            const data = await call({ action: 'importStatus', jobId: importJob.jobId });
            setImportJob(data);
            if (data.state !== 'running') {
              setImportMessage(
                data.state === 'done'
                  ? '导入完成：新文档已入库，刷新页面后即可检索。'
                  : '导入失败，请看下方日志。',
              );
            }
          } catch (error) {
            setImportMessage('查询导入进度失败：' + error.message);
          }
        }, 2500);
        return () => clearInterval(timer);
      }, [importJob?.jobId, importJob?.state]);

      const updateImport = (key, value) => {
        setImportForm((previous) => ({ ...previous, [key]: value }));
      };

      const startImport = async () => {
        setImportBusy(true);
        setImportMessage('');
        try {
          const data = await call({ action: 'import', ...importForm });
          setImportJob({ jobId: data.jobId, state: data.state, lines: [] });
          setImportMessage('已开始导入。年报解析 + 事实抽取通常需要 10–30 分钟，可以留在这个页面看进度。');
        } catch (error) {
          setImportMessage('导入失败：' + error.message);
        } finally {
          setImportBusy(false);
        }
      };

      const chooseWorkspace = async () => {
        const workspacePath = wsChoice || status?.selectedWorkspacePath || '';
        if (!workspacePath) {
          setWsMessage('请先选择一个工作区');
          return;
        }
        setWsBusy(true);
        setWsMessage('');
        try {
          const data = await call({ action: 'choose', workspacePath });
          setStatus(data);
          setWsMessage('已选择：' + data.home + '。重载插件（或重启 DSH）后 MCP 才会使用新目录；之后点“初始化知识库”。');
        } catch (error) {
          setWsMessage('保存工作区失败：' + error.message);
        } finally {
          setWsBusy(false);
        }
      };

      const initLibrary = async () => {
        setWsBusy(true);
        setWsMessage('');
        try {
          const data = await call({ action: 'init' });
          setStatus((previous) =>
            previous ? { ...previous, home: data.home, initialized: data.initialized } : previous,
          );
          setWsMessage(data.message + (data.output ? '\n' + data.output.slice(-800) : ''));
        } catch (error) {
          setWsMessage('初始化失败：' + error.message);
        } finally {
          setWsBusy(false);
        }
      };

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
          const data = await call({ action: 'save', values: secrets });
          setStatus(data);
          setSecrets({});
          setValues((previous) => ({
            ...previous,
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
            '这里配置密钥、模型地址与知识库位置。保存密钥后立即生效：FinKB 每次调用都会重新读取 .env，不需要重启 DSH 或 MCP 进程。切换知识库目录后需要重载插件，再点“初始化知识库”。',
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
                  h(
                    'div',
                    { key: 'h' },
                    '知识库目录（部署期 FINKB_HOME 决定，运行期不可改）：' + status.home,
                  ),
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
          status && (!configured.siliconflowApiKey || !configured.mineruToken)
            ? h(
                'div',
                {
                  key: 'warn',
                  style: {
                    padding: '10px 12px',
                    borderRadius: '10px',
                    border: '0.5px solid ' + T.warn,
                    color: T.warn,
                    fontSize: '12px',
                    lineHeight: 1.7,
                  },
                },
                [
                  '建议补充：',
                  configured.siliconflowApiKey ? null : '未填 SiliconFlow Key → 检索会退化为纯关键词，语义召回明显变差；',
                  configured.mineruToken ? null : '未填 MinerU Token → 新导入年报的表格/扫描页解析质量明显下降。',
                ],
              )
            : null,
          h('div', { key: 'wh', style: { fontSize: '14px', fontWeight: 700, marginTop: '6px' } }, '知识库位置'),
          h(
            'p',
            { key: 'wd', style: { margin: 0, color: T.label2, fontSize: '13px', lineHeight: 1.6 } },
            '数据资产（raw/data/config/wiki）放在所选工作区的 finkb-data/ 目录；插件代码仍走 npm/pip，不写进工作区。首次使用建议先选一个干净的工作区。',
          ),
          status
            ? [
                h(
                  'div',
                  { key: 'ws', style: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' } },
                  [
                    h(
                      'select',
                      {
                        key: 'sel',
                        value: wsChoice,
                        onChange: (event) => setWsChoice(event.target.value),
                        style: { ...inputStyle, width: 'auto', minWidth: '260px', maxWidth: '420px' },
                      },
                      [
                        h('option', { key: '', value: '' }, '— 选择 DSH 工作区 —'),
                        ...(status.workspaces ?? []).map((workspace) =>
                          h(
                            'option',
                            { key: workspace.id || workspace.path, value: workspace.path },
                            (workspace.title || workspace.path) + '（' + workspace.path + '）',
                          ),
                        ),
                      ],
                    ),
                    h(
                      'button',
                      {
                        key: 'apply',
                        type: 'button',
                        disabled: wsBusy || !wsChoice,
                        onClick: chooseWorkspace,
                        style: {
                          height: '32px',
                          padding: '0 14px',
                          borderRadius: '8px',
                          border: '0.5px solid ' + T.border,
                          background: T.bgNested,
                          color: T.label,
                          fontSize: '13px',
                          cursor: wsBusy ? 'default' : 'pointer',
                        },
                      },
                      wsBusy ? '处理中…' : '使用该工作区',
                    ),
                    h(
                      'button',
                      {
                        key: 'init',
                        type: 'button',
                        disabled: wsBusy,
                        onClick: initLibrary,
                        style: {
                          height: '32px',
                          padding: '0 14px',
                          borderRadius: '8px',
                          border: '0.5px solid ' + T.border,
                          background: T.bgNested,
                          color: T.label,
                          fontSize: '13px',
                          cursor: wsBusy ? 'default' : 'pointer',
                        },
                      },
                      '初始化知识库',
                    ),
                  ],
                ),
                h(
                  'div',
                  {
                    key: 'wmsg',
                    style: { fontSize: '12px', color: T.label2, lineHeight: 1.6, whiteSpace: 'pre-wrap' },
                  },
                  wsMessage ||
                    ((status.workspaces ?? []).length === 0
                      ? 'DSH 里还没有工作区：请先在左侧创建工作区，然后回到本页刷新。'
                      : '当前资产目录：' +
                        status.home +
                        (status.selectedWorkspacePath ? '（来自工作区 ' + status.selectedWorkspacePath + '）' : '（尚未选择工作区）')),
                ),
              ]
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
          h('div', { key: 'ih', style: { fontSize: '14px', fontWeight: 700, marginTop: '6px' } }, '导入年报'),
          h(
            'p',
            { key: 'id', style: { margin: 0, color: T.label2, fontSize: '13px', lineHeight: 1.6 } },
            '这里不发文件上传：PDF 留在本机，导入时会复制到知识库的 raw/ 目录，再解析入库。支持 A 股年报/半年报/一季报/三季报 PDF。',
          ),
          status?.rawDir
            ? h(
                'p',
                { key: 'ir', style: { margin: 0, color: T.label2, fontSize: '12px', lineHeight: 1.6, wordBreak: 'break-all' } },
                'PDF 副本存放：' + status.rawDir,
              )
            : null,
          h('div', { key: 'ip', style: { display: 'flex', flexDirection: 'column', gap: '8px' } }, [
            Field({
              field: { key: 'pdfPath', label: '本机 PDF 绝对路径', hint: '例如 /home/你/下载/600519_2025_年报.pdf' },
              value: importForm.pdfPath,
              placeholder: '/绝对路径/xxx.pdf',
              onChange: (key, value) => updateImport('pdfPath', value),
            }),
            h('div', { key: 'row', style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } }, [
              Field({
                field: { key: 'company', label: '公司代码', hint: '6 位数字' },
                value: importForm.company,
                placeholder: '600519',
                onChange: (key, value) => updateImport('company', value),
              }),
              Field({
                field: { key: 'period', label: '期间', hint: '2025 / 2025H1 / 2025Q1 / 2025Q3' },
                value: importForm.period,
                placeholder: '2025',
                onChange: (key, value) => updateImport('period', value),
              }),
              Field({
                field: { key: 'reportType', label: '报告类型', hint: '年报 / 半年报 / 一季报 / 三季报' },
                value: importForm.reportType,
                placeholder: '年报',
                onChange: (key, value) => updateImport('reportType', value),
              }),
            ]),
          ]),
          h(
            'div',
            { key: 'ia', style: { display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' } },
            [
              h(
                'button',
                {
                  key: 'go',
                  type: 'button',
                  disabled: importBusy || importJob?.state === 'running',
                  onClick: startImport,
                  style: {
                    height: '32px',
                    padding: '0 16px',
                    borderRadius: '8px',
                    border: '0.5px solid ' + T.border,
                    background: importBusy || importJob?.state === 'running' ? T.bgNested : T.accent,
                    color: importBusy || importJob?.state === 'running' ? T.label2 : '#fff',
                    fontSize: '13px',
                    fontWeight: 600,
                    cursor: importBusy || importJob?.state === 'running' ? 'default' : 'pointer',
                  },
                },
                importJob?.state === 'running' ? '导入中…' : '开始导入',
              ),
              importMessage
                ? h(
                    'span',
                    { key: 'im', style: { fontSize: '12px', color: T.label2, lineHeight: 1.5 } },
                    importMessage,
                  )
                : null,
            ],
          ),
          importJob?.lines?.length
            ? h(
                'pre',
                {
                  key: 'ilog',
                  style: {
                    margin: 0,
                    padding: '10px',
                    borderRadius: '8px',
                    border: '0.5px solid ' + T.border,
                    background: T.bgNested,
                    color: T.label2,
                    fontSize: '11px',
                    lineHeight: 1.5,
                    maxHeight: '180px',
                    overflow: 'auto',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                  },
                },
                importJob.lines.slice(-60).join('\n'),
              )
            : null,
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
