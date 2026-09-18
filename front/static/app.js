/* ============================================================================
   OnCall 前端应用
   ----------------------------------------------------------------------------
   与后端现有接口 100% 兼容：
     · POST /api/chat          body {"Id","Question"}       → {code,message,data:{success,answer,errorMessage}}
     · POST /api/chat_stream   body {"Id","Question"}       → SSE(type: content|done|error)
     · POST /api/chat/clear    body {"session_id"}          → {status,message}
     · GET  /api/chat/session/{id}                          → {session_id,message_count,history:[{role,content,timestamp}]}
     · POST /api/upload        multipart 字段 file，可选切片参数
     ·                         strategy / chunk_size / chunk_overlap / parent_size
     ·                         / rules / parent_rules（JSON 规则列表，分片依据）
     ·                         → {code,message,data:{filename,file_path,size,indexed,strategy,stats,chunks,total,truncated}}
     · POST /api/aiops         body {"session_id"}          → SSE(type: status|plan|step_complete|report|complete|done|error)
     · GET  /api/config                                     → {code,message,data:{...}}
   所有请求使用同源相对路径 /api，不写死端口。
   ========================================================================== */

(function () {
  'use strict';

  var API = '/api';
  var STATS_KEY = 'chatHistories';   // 与旧版保持同一个 localStorage key，历史不丢
  var MAX_HISTORY = 50;
  var MAX_UPLOAD = 10 * 1024 * 1024;   // 与服务端 /api/upload 的 MAX_FILE_SIZE 保持一致
  var FILE_EXT = ['.txt', '.md', '.pdf', '.pptx', '.docx', '.png', '.jpg', '.jpeg', '.bmp', '.webp'];

  function el(id) { return document.getElementById(id); }

  function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text === null || text === undefined ? '' : String(text);
    return div.innerHTML;
  }

  function genSessionId() {
    return 'session_' + Math.random().toString(36).slice(2, 11) + '_' + Date.now();
  }

  function formatSize(bytes) {
    if (bytes === null || bytes === undefined) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }

  function makeSpinner(extraClass) {
    return '<svg class="spin ' + (extraClass || '') + '" width="16" height="16" viewBox="0 0 24 24" ' +
      'fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true">' +
      '<path d="M12 3a9 9 0 1 0 9 9"/></svg>';
  }

  /* 消息操作行图标 */
  var ICON_COPY = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  var ICON_REGEN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>';
  /* 钉钉推送卡片：展开/收起箭头 */
  var ICON_CHEVRON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="m6 9 6 6 6-6"/></svg>';
  var ICON_CLOSE = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';

  /* ========================================================================
     分片依据（规则）——类型、默认值与渲染
     ====================================================================== */

  var RULE_TYPES = [
    { value: 'separator', label: '自定义分隔符' },
    { value: 'heading', label: '按标题层级' },
    { value: 'length', label: '按固定长度' }
  ];
  var RULE_TYPE_DEFAULT = { separator: '', heading: '#', length: '300' };
  var HEADING_LEVELS = ['#', '##', '###'];
  var MAX_RULES = 8;

  /* 各切片方式的默认值：切换时按这套套用，并各自记住用户填过的值 */
  function defaultStrategyState() {
    return {
      general: {
        rules: [{ type: 'separator', value: '\\n\\n' }],
        chunkSize: '800',
        overlap: '100'
      },
      chain: {
        rules: [
          { type: 'separator', value: '\\n\\n' },
          { type: 'separator', value: '\\n' },
          { type: 'length', value: '800' }
        ],
        chunkSize: '800',
        overlap: '100'
      },
      loop: {
        rules: [],
        chunkSize: '500',
        overlap: '50'
      },
      parent_child: {
        parentRules: [{ type: 'separator', value: '\\n\\n' }],
        childRules: [{ type: 'separator', value: '\\n' }],
        chunkSize: '400',
        parentSize: '800'
      }
    };
  }

  /* 一条分片依据的 HTML：类型下拉 + 取值控件 +（可选）删除按钮 */
  function ruleRowHtml(rule, index, allowDelete) {
    var type = (rule && rule.type) || 'separator';
    var value = rule && rule.value !== undefined && rule.value !== null ? String(rule.value) : '';

    var typeOptions = RULE_TYPES.map(function (item) {
      return '<option value="' + item.value + '"' + (item.value === type ? ' selected' : '') + '>' +
        escapeHtml(item.label) + '</option>';
    }).join('');

    var valueControl;
    if (type === 'heading') {
      var level = value || '#';
      valueControl = '<select class="input rule__value" data-rule-value>' +
        HEADING_LEVELS.map(function (item) {
          return '<option value="' + item + '"' + (item === level ? ' selected' : '') + '>' +
            escapeHtml(item) + '</option>';
        }).join('') + '</select>';
    } else if (type === 'length') {
      valueControl = '<input class="input rule__value" data-rule-value type="number" ' +
        'min="50" max="4000" step="50" value="' + escapeHtml(value || RULE_TYPE_DEFAULT.length) + '">';
    } else {
      valueControl = '<input class="input rule__value" data-rule-value type="text" ' +
        'placeholder="例：\\n\\n 或 @@@ 或 \\n" value="' + escapeHtml(value) + '">';
    }

    return '<div class="rule" data-rule-index="' + index + '">' +
      '<select class="input rule__type" data-rule-type aria-label="依据类型">' + typeOptions + '</select>' +
      valueControl +
      (allowDelete
        ? '<button class="icon-btn rule__del" type="button" data-rule-del aria-label="删除该依据">' +
          ICON_CLOSE + '</button>'
        : '') +
      '</div>';
  }

  /* 从容器里按顺序读出当前界面上的规则（以 DOM 为准，避免与内存状态不同步） */
  function collectRules(container) {
    if (!container) return [];
    var rows = container.querySelectorAll('.rule');
    var out = [];
    Array.prototype.forEach.call(rows, function (row) {
      var typeNode = row.querySelector('[data-rule-type]');
      var valueNode = row.querySelector('[data-rule-value]');
      if (!typeNode || !valueNode) return;
      out.push({ type: typeNode.value, value: valueNode.value });
    });
    return out;
  }

  /* 抽屉互斥：抽屉与窄屏侧栏都是 fixed + z-index:40，同时打开会互相遮挡
     （z-index 相同时 DOM 靠后的那个在上层，所以后开的抽屉反而看不见） */
  function closeOtherPanels(current) {
    Array.prototype.forEach.call(document.querySelectorAll('.drawer.is-open'), function (node) {
      if (node === current) return;
      node.classList.remove('is-open');
      node.setAttribute('aria-hidden', 'true');
    });
    // 打开抽屉时顺手收起窄屏侧栏，避免同源问题
    var app = el('app');
    if (app) app.classList.remove('is-nav-open');
  }

  /* ========================================================================
     主应用：对话 / 上传 / AI Ops / 会话历史
     ====================================================================== */

  class App {
    constructor() {
      this.sessionId = genSessionId();
      this.isStreaming = false;
      this.messages = [];
      this.histories = this.loadHistories();
      this.isFromHistory = false;

      // 上传附件面板：选中的文件 + 本次切片结果。
      // chunk 正文只放内存变量，不写 localStorage、不进会话历史，刷新页面即消失（文档保密要求）。
      this.pendingFile = null;
      this.uploadStrategy = 'general';
      this.uploadChunks = [];
      this.uploadMeta = null;
      // 各切片方式各自的参数（切换时先存旧值再套用新值，来回切不丢）
      this.strategyState = defaultStrategyState();

      this.refs = {
        app: el('app'),
        main: el('main'),
        sidebar: el('sidebar'),
        backdrop: el('backdrop'),
        navOpenBtn: el('navOpenBtn'),
        newChatBtn: el('newChatBtn'),
        historyList: el('chatHistoryList'),
        aiOpsBtn: el('aiOpsSidebarBtn'),
        thread: el('chatMessages'),
        welcome: el('welcomeGreeting'),
        input: el('messageInput'),
        sendBtn: el('sendButton'),
        uploadPanelBtn: el('uploadPanelBtn'),
        uploadDrawer: el('uploadDrawer'),
        uploadCloseBtn: el('uploadCloseBtn'),
        uploadPick: el('uploadPick'),
        uploadFileInput: el('fileInput'),
        uploadFileName: el('uploadFileName'),
        uploadRemoveBtn: el('uploadRemoveBtn'),
        uploadSection: el('uploadSection'),
        strategySeg: el('uploadStrategySeg'),
        rulesField: el('uploadRulesField'),
        rulesLabel: el('uploadRulesLabel'),
        rulesHint: el('uploadRulesHint'),
        rules: el('uploadRules'),
        addRuleBtn: el('uploadAddRuleBtn'),
        parentRulesField: el('uploadParentRulesField'),
        parentRules: el('uploadParentRules'),
        chunkSizeLabel: el('uploadChunkSizeLabel'),
        chunkSizeInput: el('uploadChunkSize'),
        parentSizeField: el('uploadParentSizeField'),
        parentSizeInput: el('uploadParentSize'),
        overlapField: el('uploadOverlapField'),
        overlapInput: el('uploadOverlap'),
        uploadSubmitBtn: el('uploadSubmitBtn'),
        uploadPreview: el('uploadPreview'),
        searchBtn: el('historySearchBtn'),
        searchInput: el('historySearchInput'),
        overlay: el('loadingOverlay'),
        toasts: el('toastWrap')
      };

      this.bind();
      this.initMarkdown();
      this.initNotifyStream();
      this.renderHistories();
      this.showWelcome();
      this.autoGrow();
      this.applyStrategy(this.uploadStrategy, true);   // 初始渲染切片设置（skipStore：别回收空界面）
    }

    /* ------------------------------ 事件 ------------------------------ */

    bind() {
      var r = this.refs;
      var on = function (node, type, fn) { if (node) node.addEventListener(type, fn); };

      on(r.newChatBtn, 'click', () => this.newChat());
      on(r.aiOpsBtn, 'click', () => this.triggerAIOps());

      on(r.navOpenBtn, 'click', () => this.toggleNav(true));
      on(r.backdrop, 'click', () => this.toggleNav(false));

      on(r.sendBtn, 'click', () => this.sendMessage());
      on(r.input, 'keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.sendMessage(); }
      });
      on(r.input, 'input', () => this.autoGrow());

      on(r.searchBtn, 'click', () => this.toggleSearch());
      on(r.searchInput, 'input', () => this.renderHistories());

      on(r.thread, 'click', (e) => this.onThreadClick(e));

      on(r.uploadPanelBtn, 'click', () => this.toggleUploadPanel());
      on(r.uploadCloseBtn, 'click', () => this.closeUploadPanel());
      on(r.uploadPick, 'click', () => r.uploadFileInput && r.uploadFileInput.click());
      on(r.uploadFileInput, 'change', (e) => this.handleFileSelect(e));
      on(r.uploadRemoveBtn, 'click', () => this.clearUploadFile());
      on(r.uploadSubmitBtn, 'click', () => this.submitUpload());
      on(r.strategySeg, 'click', (e) => this.onStrategyClick(e));
      on(r.addRuleBtn, 'click', () => this.addRule(this.currentRulesKey()));
      on(r.rules, 'click', (e) => this.onRuleDelete(e, this.currentRulesKey()));
      on(r.rules, 'change', (e) => this.onRuleChange(e, this.currentRulesKey()));
      on(r.parentRules, 'click', (e) => this.onRuleDelete(e, 'parentRules'));
      on(r.parentRules, 'change', (e) => this.onRuleChange(e, 'parentRules'));
      on(r.uploadPreview, 'click', (e) => this.onPreviewClick(e));
      on(r.uploadDrawer, 'click', (e) => { if (e.target === r.uploadDrawer) this.closeUploadPanel(); });

      on(r.historyList, 'click', (e) => {
        var del = e.target.closest('.history__del');
        if (del) { e.stopPropagation(); this.deleteChatHistory(del.dataset.id); return; }
        var item = e.target.closest('.history__item');
        if (item) this.loadChatHistory(item.dataset.id);
      });
    }

    toggleNav(open) {
      if (this.refs.app) this.refs.app.classList.toggle('is-nav-open', !!open);
    }

    autoGrow() {
      var input = this.refs.input;
      if (!input) return;
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 160) + 'px';
    }

    /* ---------------------------- 历史搜索 ---------------------------- */

    toggleSearch() {
      var input = this.refs.searchInput;
      if (!input) return;
      if (input.hidden) {
        input.hidden = false;
        input.focus();
      } else {
        input.value = '';
        input.hidden = true;
        this.renderHistories();
      }
    }

    searchKeyword() {
      var input = this.refs.searchInput;
      return ((input && input.value) || '').trim().toLowerCase();
    }

    updateSendState() {
      if (this.refs.sendBtn) this.refs.sendBtn.disabled = this.isStreaming;
    }

    /* --------------------------- Markdown --------------------------- */

    initMarkdown() {
      var self = this;
      (function wait() {
        if (typeof marked === 'undefined') { setTimeout(wait, 100); return; }
        try {
          marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
          if (typeof hljs !== 'undefined') {
            marked.setOptions({
              highlight: function (code, lang) {
                if (lang && hljs.getLanguage(lang)) {
                  try { return hljs.highlight(code, { language: lang }).value; }
                  catch (err) { console.error('代码高亮失败:', err); }
                }
                return code;
              }
            });
          }
        } catch (e) {
          console.error('Markdown 配置失败:', e);
        }
      })();
    }

    renderMarkdown(content) {
      if (!content) return '';
      if (typeof marked === 'undefined') return escapeHtml(content);
      try { return marked.parse(content); }
      catch (e) { console.error('Markdown 渲染失败:', e); return escapeHtml(content); }
    }

    /* 渲染后的装饰：代码块工具条、表格容器
       流式每收到一段都会整体重绘 innerHTML，所以每次高亮前都要重新装饰一遍 */
    decorateContent(container) {
      this.decorateCodeBlocks(container);
      this.decorateTables(container);
    }

    /* 代码块：外层包 .code-block，顶部插一条「语言名 + 复制」工具条 */
    decorateCodeBlocks(container) {
      Array.prototype.forEach.call(container.querySelectorAll('pre'), (pre) => {
        if (pre.parentElement && pre.parentElement.classList.contains('code-block')) return;
        var code = pre.querySelector('code');
        if (!code) return;

        var matched = /language-([\w-]+)/.exec(code.className || '');
        var lang = matched ? matched[1] : '';
        if (!lang || lang === 'plaintext' || lang === 'text') lang = '代码';

        var block = document.createElement('div');
        block.className = 'code-block';
        pre.parentNode.insertBefore(block, pre);
        block.appendChild(pre);

        var bar = document.createElement('div');
        bar.className = 'code-block__bar';
        bar.innerHTML = '<span class="code-block__lang">' + escapeHtml(lang) + '</span>' +
          '<button class="code-block__copy" type="button" data-copy-code title="复制代码">' +
          ICON_COPY + '<span>复制</span></button>';
        block.insertBefore(bar, pre);
      });
    }

    /* 表格：外层包 .md__table-wrap，拿到圆角与横向滚动 */
    decorateTables(container) {
      Array.prototype.forEach.call(container.querySelectorAll('table'), (table) => {
        if (table.parentElement && table.parentElement.classList.contains('md__table-wrap')) return;
        var wrap = document.createElement('div');
        wrap.className = 'md__table-wrap';
        table.parentNode.insertBefore(wrap, table);
        wrap.appendChild(table);
      });
    }

    highlight(container) {
      if (!container) return;
      this.decorateContent(container);          // 先装饰
      if (typeof hljs === 'undefined') return;  // hljs 没加载也不能跳过上面的装饰
      try {
        container.querySelectorAll('pre code').forEach(function (block) {
          if (!block.classList.contains('hljs')) hljs.highlightElement(block);
        });
      } catch (e) { console.error('代码高亮失败:', e); }
    }

    /* ------------------------- 钉钉推送记录订阅 ------------------------- */

    /* 订阅「已推送到钉钉的消息」：浏览器在线才连得上；断开由 EventSource 自动重连 */
    initNotifyStream() {
      if (typeof EventSource === 'undefined') return;
      try {
        var es = new EventSource('/api/notify/stream');
        es.onmessage = (e) => {
          var payload = null;
          try { payload = JSON.parse(e.data); } catch (err) { return; }
          if (payload && payload.type === 'dingtalk') this.addDingtalkCard(payload.data);
        };
        es.onerror = () => { /* EventSource 自带重连，这里不需要处理 */ };
        this.notifySource = es;
      } catch (e) {
        console.error('通知订阅失败:', e);
      }
    }

    /* 钉钉推送记录：对话区一张默认收起的卡片
       只存在于当前画面，不进 this.messages、不写 localStorage */
    addDingtalkCard(item) {
      if (!item || !this.refs.thread) return;

      var card = document.createElement('article');
      card.className = 'notice notice--dingtalk';
      card.innerHTML =
        '<button class="notice__head" type="button" data-notice-toggle aria-expanded="false">' +
          '<span class="notice__badge">钉钉</span>' +
          '<span class="notice__title">' + escapeHtml(item.title || '已推送钉钉') + '</span>' +
          '<span class="notice__meta">' + escapeHtml(item.pushed_at || '') + '</span>' +
          '<span class="notice__chevron" aria-hidden="true">' + ICON_CHEVRON + '</span>' +
        '</button>' +
        '<div class="notice__body md" hidden></div>';

      var body = card.querySelector('.notice__body');
      body.innerHTML = this.renderMarkdown(item.text || '');
      this.highlight(body);          // 复用代码块工具条 / 表格包裹

      this.refs.thread.appendChild(card);
      this.hideWelcome();
      this.scrollBottom();
      this.notify('已推送到钉钉，可在对话中展开查看', 'success');
    }

    /* ---------------------------- 消息渲染 ---------------------------- */

    addMessage(type, content, isStreaming, record) {
      var isAssistant = type !== 'user';

      if (record !== false && !isStreaming && content) {
        this.messages.push({
          type: isAssistant ? 'assistant' : 'user',
          content: content,
          timestamp: new Date().toISOString()
        });
      }

      var wrap = document.createElement('article');
      wrap.className = 'msg msg--' + (isAssistant ? 'assistant' : 'user');

      var body = document.createElement('div');
      body.className = 'msg__content';
      if (isAssistant) body.classList.add('md');
      if (isStreaming) body.classList.add('is-streaming');
      if (isAssistant && !isStreaming) body.innerHTML = this.renderMarkdown(content);
      else body.textContent = content || '';

      wrap.appendChild(body);
      if (!isStreaming) wrap.appendChild(this.buildActions(isAssistant));
      this.refs.thread.appendChild(wrap);

      if (isAssistant && !isStreaming) this.highlight(body);

      this.hideWelcome();
      this.syncRegenButtons();
      this.scrollBottom();
      return wrap;
    }

    /* 「重新生成」只对最后一条回答有意义：切换按钮归属，避免点了旧回答导致 DOM 与消息记录错位 */
    syncRegenButtons() {
      var nodes = this.refs.thread.querySelectorAll('.msg--assistant');
      for (var i = 0; i < nodes.length; i++) {
        var btn = nodes[i].querySelector('.msg__action[data-act="regen"]');
        if (btn && i !== nodes.length - 1) btn.remove();
      }
    }

    /* 消息下方操作行：复制（两种角色）、重新生成（仅助手） */
    buildActions(isAssistant) {
      var row = document.createElement('div');
      row.className = 'msg__actions';
      row.innerHTML =
        '<button class="msg__action" type="button" data-act="copy" title="复制">' +
        ICON_COPY + '<span>复制</span></button>' +
        (isAssistant
          ? '<button class="msg__action" type="button" data-act="regen" title="重新生成">' +
            ICON_REGEN + '<span>重新生成</span></button>'
          : '');
      return row;
    }

    /* 事件委托：钉钉推送卡片展开 / 复制代码 / 复制整条回答 / 重新生成 */
    onThreadClick(event) {
      var noticeHead = event.target.closest('[data-notice-toggle]');
      if (noticeHead) {
        var noticeCard = noticeHead.closest('.notice');
        var noticeBody = noticeCard && noticeCard.querySelector('.notice__body');
        if (noticeBody) {
          var willOpen = noticeBody.hidden;
          noticeBody.hidden = !willOpen;
          noticeCard.classList.toggle('is-open', willOpen);
          noticeHead.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
        }
        return;
      }

      var codeBtn = event.target.closest('[data-copy-code]');
      if (codeBtn) {
        var block = codeBtn.closest('.code-block');
        var code = block && block.querySelector('pre code');
        if (code) this.copyText(code.innerText);
        return;
      }

      var btn = event.target.closest('.msg__action');
      if (!btn) return;
      var wrap = btn.closest('.msg');
      var body = wrap && wrap.querySelector('.msg__content');
      var text = body ? body.innerText : '';
      if (btn.dataset.act === 'copy') this.copyText(text);
      else if (btn.dataset.act === 'regen') this.regenerate(wrap);
    }

    copyText(text) {
      if (!text) return;
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text)
          .then(() => this.notify('已复制到剪贴板', 'success'))
          .catch(() => this.copyTextFallback(text));
        return;
      }
      this.copyTextFallback(text);
    }

    copyTextFallback(text) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        ta.remove();
        this.notify(ok ? '已复制到剪贴板' : '复制失败，请手动选择文本', ok ? 'success' : 'error');
      } catch (e) {
        this.notify('复制失败: ' + e.message, 'error');
      }
    }

    /* 重新生成：重发这条回答对应的用户提问 */
    regenerate(wrap) {
      if (this.isStreaming) return this.notify('请等待当前对话完成', 'warning');

      var question = '';
      for (var i = this.messages.length - 1; i >= 0; i--) {
        if (this.messages[i].type === 'user') { question = this.messages[i].content; break; }
      }
      if (!question) return this.notify('找不到对应的提问，无法重新生成', 'warning');

      if (wrap) wrap.remove();
      while (this.messages.length && this.messages[this.messages.length - 1].type === 'assistant') {
        this.messages.pop();
      }

      this.isStreaming = true;
      this.updateSendState();
      this.sendStream(question)
        .catch((e) => this.addMessage('assistant', '抱歉，重新生成失败：' + e.message))
        .finally(() => {
          this.isStreaming = false;
          this.updateSendState();
          this.saveCurrentChat();
          this.renderHistories();
        });
    }

    addLoadingMessage(text) {
      var wrap = document.createElement('article');
      wrap.className = 'msg msg--assistant';
      wrap.innerHTML =
        '<div class="msg__content"><span class="inline-loading">' +
        makeSpinner() + '<span>' + escapeHtml(text) + '</span></span></div>';
      this.refs.thread.appendChild(wrap);
      this.hideWelcome();
      this.scrollBottom();
      return wrap;
    }

    paintStream(holder, text) {
      var body = holder && holder.querySelector('.msg__content');
      if (!body) return;
      body.innerHTML = this.renderMarkdown(text);
      this.highlight(body);
      this.scrollBottom();
    }

    finishStream(holder, text) {
      var body = holder && holder.querySelector('.msg__content');
      if (body) {
        body.classList.remove('is-streaming');
        body.innerHTML = this.renderMarkdown(text);
        this.highlight(body);
      }
      if (text && text.trim()) {
        this.messages.push({ type: 'assistant', content: text, timestamp: new Date().toISOString() });
      }
      this.scrollBottom();
    }

    failStream(holder, text) {
      var body = holder && holder.querySelector('.msg__content');
      if (body) {
        body.classList.remove('is-streaming');
        body.innerHTML = this.renderMarkdown(text);
        this.highlight(body);
      }
      if (holder) holder.classList.add('msg--error');
      this.scrollBottom();
    }

    scrollBottom() {
      var t = this.refs.thread;
      if (t) t.scrollTop = t.scrollHeight;
    }

    /* 空会话 / 有消息两态：空态给主区挂 is-empty，让欢迎语与输入卡片整体居中 */
    showWelcome() {
      if (this.refs.welcome) this.refs.welcome.hidden = false;
      if (this.refs.main) this.refs.main.classList.add('is-empty');
    }

    hideWelcome() {
      if (this.refs.welcome) this.refs.welcome.hidden = true;
      if (this.refs.main) this.refs.main.classList.remove('is-empty');
    }

    /* ---------------------------- 会话历史 ---------------------------- */

    loadHistories() {
      try {
        var raw = localStorage.getItem(STATS_KEY);
        return raw ? JSON.parse(raw) : [];
      } catch (e) {
        console.error('加载历史对话失败:', e);
        return [];
      }
    }

    saveHistories() {
      try { localStorage.setItem(STATS_KEY, JSON.stringify(this.histories)); }
      catch (e) { console.error('保存历史对话失败:', e); }
    }

    saveCurrentChat() {
      if (!this.messages.length) return;
      var firstUser = this.messages.find(function (m) { return m.type === 'user'; });
      var title = firstUser ? firstUser.content.slice(0, 30) : '新对话';
      if (firstUser && firstUser.content.length > 30) title += '...';
      var now = new Date().toISOString();
      var idx = this.histories.findIndex((h) => h.id === this.sessionId);

      if (idx >= 0) {
        this.histories[idx].messages = this.messages.slice();
        this.histories[idx].updatedAt = now;
        if (!this.histories[idx].title) this.histories[idx].title = title;
      } else {
        this.histories.unshift({
          id: this.sessionId,
          title: title,
          messages: this.messages.slice(),
          createdAt: now,
          updatedAt: now
        });
        this.histories = this.histories.slice(0, MAX_HISTORY);
      }
      this.saveHistories();
    }

    /* 时间分组：今天 / 昨天 / 7 天内 / 30 天内 / 更早 */
    groupLabel(iso) {
      var time = new Date(iso).getTime();
      if (!time) return '更早';
      var startOfToday = new Date();
      startOfToday.setHours(0, 0, 0, 0);
      var today0 = startOfToday.getTime();
      var day = 24 * 60 * 60 * 1000;
      if (time >= today0) return '今天';
      if (time >= today0 - day) return '昨天';
      if (time >= today0 - 7 * day) return '7 天内';
      if (time >= today0 - 30 * day) return '30 天内';
      return '更早';
    }

    renderHistories() {
      var list = this.refs.historyList;
      if (!list) return;

      var keyword = this.searchKeyword();
      var items = this.histories.filter(function (h) {
        if (!keyword) return true;
        return (h.title || '').toLowerCase().indexOf(keyword) >= 0;
      });

      if (!items.length) {
        list.innerHTML = '<p class="history__empty">' +
          (keyword ? '没有匹配的对话' : '还没有对话') + '</p>';
        return;
      }

      var groups = {};
      items.forEach((h) => {
        var label = this.groupLabel(h.updatedAt || h.createdAt);
        (groups[label] = groups[label] || []).push(h);
      });

      var html = '';
      ['今天', '昨天', '7 天内', '30 天内', '更早'].forEach((label) => {
        var bucket = groups[label];
        if (!bucket || !bucket.length) return;
        html += '<p class="history__group-title">' + label + '</p>';
        html += bucket.map((h) => {
          var active = h.id === this.sessionId ? ' is-active' : '';
          return '<div class="history__item' + active + '" data-id="' + escapeHtml(h.id) + '">' +
            '<button class="history__title" type="button">' + escapeHtml(h.title || '未命名对话') + '</button>' +
            '<button class="history__del" type="button" data-id="' + escapeHtml(h.id) + '" title="删除">' +
            '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true">' +
            '<path d="M18 6 6 18M6 6l12 12"/></svg></button></div>';
        }).join('');
      });
      list.innerHTML = html;
    }

    newChat() {
      if (this.isStreaming) return this.notify('请等待当前对话完成后再新建对话', 'warning');
      this.saveCurrentChat();

      this.messages = [];
      this.isFromHistory = false;
      this.sessionId = genSessionId();
      if (this.refs.thread) this.refs.thread.innerHTML = '';
      this.showWelcome();
      if (this.refs.input) { this.refs.input.value = ''; this.autoGrow(); }
      this.renderHistories();
    }

    async loadChatHistory(id) {
      var history = this.histories.find((h) => h.id === id);
      if (!history) return;
      if (this.messages.length && this.sessionId !== id) this.saveCurrentChat();

      this.sessionId = id;
      this.isFromHistory = true;
      this.messages = [];
      if (this.refs.thread) this.refs.thread.innerHTML = '';
      this.showWelcome();

      var loaded = false;
      try {
        var res = await fetch(API + '/chat/session/' + encodeURIComponent(id));
        if (res.ok) {
          var data = await res.json();
          var items = data.history || [];
          if (items.length) {
            items.forEach((m) => this.addMessage(m.role === 'user' ? 'user' : 'assistant', m.content));
            loaded = true;
          }
        } else {
          console.warn('从后端加载历史失败，使用本地缓存');
        }
      } catch (e) {
        console.error('加载会话历史失败:', e);
      }

      if (!loaded) {
        (history.messages || []).forEach((m) => this.addMessage(m.type, m.content));
      }

      this.renderHistories();
      this.scrollBottom();
    }

    async deleteChatHistory(id) {
      try {
        var res = await fetch(API + '/chat/clear', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: id })
        });
        if (!res.ok) throw new Error('清空会话失败');
        var result = await res.json();
        if (result.status !== 'success') throw new Error(result.message || '清空会话失败');

        this.histories = this.histories.filter((h) => h.id !== id);
        this.saveHistories();

        if (this.sessionId === id) {
          this.messages = [];
          if (this.refs.thread) this.refs.thread.innerHTML = '';
          this.sessionId = genSessionId();
          this.showWelcome();
        }

        this.renderHistories();
        this.notify('会话已清空', 'success');
      } catch (e) {
        console.error('删除历史对话失败:', e);
        this.notify('删除失败: ' + e.message, 'error');
      }
    }

    /* ---------------------------- 发送消息 ---------------------------- */

    async sendMessage() {
      var text = (this.refs.input ? this.refs.input.value : '').trim();
      if (!text) return this.notify('请输入消息内容', 'warning');
      if (this.isStreaming) return this.notify('请等待当前对话完成', 'warning');

      this.addMessage('user', text);
      this.refs.input.value = '';
      this.autoGrow();

      this.isStreaming = true;
      this.updateSendState();

      try {
        await this.sendStream(text);
      } catch (e) {
        this.addMessage('assistant', '抱歉，发送消息时出现错误：' + e.message);
      } finally {
        this.isStreaming = false;
        this.updateSendState();
        this.saveCurrentChat();
        this.renderHistories();
      }
    }

    async sendStream(text) {
      var holder = this.addMessage('assistant', '', true);
      var full = '';

      var res = await fetch(API + '/chat_stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ Id: this.sessionId, Question: text })
      });
      if (!res.ok) throw new Error('HTTP 错误: ' + res.status);

      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';

      try {
        while (true) {
          var step = await reader.read();
          if (step.done) break;

          buffer += decoder.decode(step.value, { stream: true });
          var lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (!line.trim()) continue;
            if (line.startsWith('id:') || line.startsWith('event:')) continue;
            if (!line.startsWith('data:')) continue;

            var raw = line.slice(5).trim();
            if (raw === '[DONE]') { this.finishStream(holder, full); return; }

            var msg;
            try { msg = JSON.parse(raw); }
            catch (err) { full += raw; this.paintStream(holder, full); continue; }

            if (typeof msg.type !== 'string') { full += raw; this.paintStream(holder, full); continue; }

            if (msg.type === 'content') {
              full += msg.data || '';
              this.paintStream(holder, full);
            } else if (msg.type === 'done') {
              this.finishStream(holder, full);
              return;
            } else if (msg.type === 'error') {
              this.failStream(holder, '错误: ' + (msg.data || '未知错误'));
              return;
            }
          }
        }
        this.finishStream(holder, full);
      } finally {
        reader.releaseLock();
      }
    }

    /* -------------------------- 上传附件面板 -------------------------- */

    handleFileSelect(event) {
      var file = event.target.files && event.target.files[0];
      if (!file) return;
      if (!this.validateFile(file)) {
        this.notify('该文件不在支持的上传类型中', 'error');
        this.refs.uploadFileInput.value = '';
        return;
      }
      if (file.size > MAX_UPLOAD) {
        this.notify('文件大小不能超过 ' + formatSize(MAX_UPLOAD), 'error');
        this.refs.uploadFileInput.value = '';
        return;
      }
      this.pendingFile = file;
      this.renderPendingFile();
    }

    validateFile(file) {
      var name = (file.name || '').toLowerCase();
      return FILE_EXT.some((ext) => name.endsWith(ext));
    }

    renderPendingFile() {
      var file = this.pendingFile;
      if (this.refs.uploadFileName) {
        this.refs.uploadFileName.textContent = file
          ? (file.name + ' · ' + formatSize(file.size))
          : '尚未选择文件';
      }
      if (this.refs.uploadRemoveBtn) this.refs.uploadRemoveBtn.hidden = !file;
      if (this.refs.uploadSubmitBtn) this.refs.uploadSubmitBtn.disabled = !file || this.isStreaming;
    }

    clearUploadFile() {
      this.pendingFile = null;
      if (this.refs.uploadFileInput) this.refs.uploadFileInput.value = '';
      this.renderPendingFile();
    }

    /* 侧栏入口当开关用：已打开时再点一次就收起 */
    toggleUploadPanel() {
      var drawer = this.refs.uploadDrawer;
      if (!drawer) return;
      if (drawer.classList.contains('is-open')) {
        this.closeUploadPanel();
        return;
      }
      this.openUploadPanel();
    }

    openUploadPanel() {
      var drawer = this.refs.uploadDrawer;
      if (!drawer) return;
      closeOtherPanels(drawer);   // 与系统设置抽屉互斥，否则会被压在下面
      drawer.classList.add('is-open');
      drawer.setAttribute('aria-hidden', 'false');
      this.renderPendingFile();
      this.renderChunkPreview();
    }

    closeUploadPanel() {
      var drawer = this.refs.uploadDrawer;
      if (!drawer) return;
      drawer.classList.remove('is-open');
      drawer.setAttribute('aria-hidden', 'true');
      // 按需求：只清屏幕视觉，不清数据 —— chunk 预览保留在内存里，只有刷新页面才消失
    }

    /* ------------------- 切片方式联动（整套切片设置随策略变化） ------------------- */

    currentRulesKey() {
      return this.uploadStrategy === 'parent_child' ? 'childRules' : 'rules';
    }

    /* 把界面上当前的值收回到当前策略的状态里（切换策略前调用） */
    storeStrategyInputs() {
      var state = this.strategyState[this.uploadStrategy];
      if (!state) return;

      var size = (this.refs.chunkSizeInput && this.refs.chunkSizeInput.value || '').trim();
      if (size) state.chunkSize = size;

      if (this.uploadStrategy === 'parent_child') {
        state.parentRules = collectRules(this.refs.parentRules);
        state.childRules = collectRules(this.refs.rules);
        var parent = (this.refs.parentSizeInput && this.refs.parentSizeInput.value || '').trim();
        if (parent) state.parentSize = parent;
        return;
      }

      if (this.uploadStrategy !== 'loop') {
        state.rules = collectRules(this.refs.rules);
      }
      if (this.refs.overlapInput && this.refs.overlapInput.isConnected) {
        var overlap = (this.refs.overlapInput.value || '').trim();
        if (overlap) state.overlap = overlap;
      }
    }

    onStrategyClick(event) {
      var btn = event.target.closest('[data-strategy]');
      if (!btn) return;
      this.applyStrategy(btn.dataset.strategy);
    }

    /* 切换切片方式：依据区条数/默认值/显隐、分片大小、重复大小一起换 */
    applyStrategy(name, skipStore) {
      if (!this.strategyState[name]) return;
      if (!skipStore) this.storeStrategyInputs();

      this.uploadStrategy = name;
      var state = this.strategyState[name];
      var isParent = name === 'parent_child';
      var isLoop = name === 'loop';

      var seg = this.refs.strategySeg;
      if (seg) {
        Array.prototype.forEach.call(seg.querySelectorAll('[data-strategy]'), function (node) {
          node.classList.toggle('is-active', node.dataset.strategy === name);
        });
      }

      // 依据区：固定窗口整块隐藏；父子切片显示父+子两组、都不可增删
      if (this.refs.rulesField) this.refs.rulesField.hidden = isLoop;
      if (this.refs.parentRulesField) this.refs.parentRulesField.hidden = !isParent;
      if (this.refs.rulesHint) this.refs.rulesHint.hidden = isParent;
      if (this.refs.addRuleBtn) this.refs.addRuleBtn.hidden = isParent;
      if (this.refs.rulesLabel) this.refs.rulesLabel.textContent = isParent ? '子块依据' : '分片依据';
      if (this.refs.chunkSizeLabel) this.refs.chunkSizeLabel.textContent = isParent ? '子块大小' : '分片大小';

      this.renderRules(this.refs.rules, isParent ? state.childRules : state.rules, !isParent);
      this.renderRules(this.refs.parentRules, isParent ? state.parentRules : [], false);

      // 数值字段
      if (this.refs.chunkSizeInput) this.refs.chunkSizeInput.value = state.chunkSize;
      if (this.refs.parentSizeField) this.refs.parentSizeField.hidden = !isParent;
      if (isParent && this.refs.parentSizeInput) this.refs.parentSizeInput.value = state.parentSize || '800';

      // 重复大小：父子切片下整行从 DOM 摘掉（不是 disabled，也不是 hidden）
      this.toggleOverlapField(!isParent);
      if (!isParent && this.refs.overlapInput) this.refs.overlapInput.value = state.overlap;
    }

    toggleOverlapField(show) {
      var field = this.refs.overlapField;
      if (!field) return;
      if (show) {
        if (!field.isConnected && this.refs.uploadSection && this.refs.uploadSubmitBtn) {
          this.refs.uploadSection.insertBefore(field, this.refs.uploadSubmitBtn);
        }
      } else if (field.isConnected) {
        field.remove();
      }
    }

    /* ------------------------------ 依据规则 ------------------------------ */

    renderRules(container, rules, allowDelete) {
      if (!container) return;
      var list = rules || [];
      container.innerHTML = list.map(function (rule, index) {
        return ruleRowHtml(rule, index, allowDelete);
      }).join('');
    }

    renderRulesByKey(key) {
      var allowDelete = this.uploadStrategy !== 'parent_child';
      var container = key === 'parentRules' ? this.refs.parentRules : this.refs.rules;
      this.renderRules(container, this.getRules(key), allowDelete);
    }

    getRules(key) {
      var state = this.strategyState[this.uploadStrategy] || {};
      var list = key === 'parentRules' ? state.parentRules : state[key];
      return list || [];
    }

    setRules(key, rules) {
      var state = this.strategyState[this.uploadStrategy];
      if (!state) return;
      if (key === 'parentRules') state.parentRules = rules;
      else if (this.uploadStrategy === 'parent_child') state.childRules = rules;
      else state.rules = rules;
    }

    addRule(key) {
      var rules = this.getRules(key).slice();
      if (rules.length >= MAX_RULES) {
        return this.notify('最多 ' + MAX_RULES + ' 条分片依据', 'warning');
      }
      rules.push({ type: 'separator', value: '' });
      this.setRules(key, rules);
      this.renderRulesByKey(key);
    }

    onRuleDelete(event, key) {
      var btn = event.target.closest('[data-rule-del]');
      if (!btn) return;
      var row = btn.closest('.rule');
      if (!row) return;
      var rules = this.getRules(key).slice();
      rules.splice(Number(row.dataset.ruleIndex), 1);
      this.setRules(key, rules);
      this.renderRulesByKey(key);
    }

    /* 切换某条依据的类型时，取值控件要跟着换（并带上该类型的默认值） */
    onRuleChange(event, key) {
      var selector = event.target.closest('[data-rule-type]');
      if (!selector) return;
      var row = selector.closest('.rule');
      if (!row) return;
      var index = Number(row.dataset.ruleIndex);
      var rules = this.getRules(key).slice();
      if (!rules[index]) return;
      rules[index] = { type: selector.value, value: RULE_TYPE_DEFAULT[selector.value] || '' };
      this.setRules(key, rules);
      this.renderRulesByKey(key);
    }

    /* 预览卡片展开 / 收起 */
    onPreviewClick(event) {
      var btn = event.target.closest('.chunk-card__toggle');
      if (!btn) return;
      var card = btn.closest('.chunk-card');
      if (!card) return;
      var open = card.classList.toggle('is-open');
      btn.textContent = open ? '收起' : '展开全文';
    }

    /* 提交前的本地校验：参数不对就没必要占用一次上传 */
    validateUploadForm() {
      var isParent = this.uploadStrategy === 'parent_child';

      var size = Number((this.refs.chunkSizeInput && this.refs.chunkSizeInput.value || '').trim());
      if (!size || size < 50 || size > 4000) return '分片大小需在 50~4000 之间';

      if (isParent) {
        var parentSize = Number((this.refs.parentSizeInput && this.refs.parentSizeInput.value || '').trim());
        if (!parentSize || parentSize <= size || parentSize > 8000) {
          return '父块大小需大于子块大小，且不超过 8000';
        }
      } else {
        var overlapValue = Number((this.refs.overlapInput && this.refs.overlapInput.value || '').trim());
        if (isNaN(overlapValue) || overlapValue < 0 || overlapValue >= size) {
          return '重复大小需大于等于 0 且小于分片大小';
        }
      }

      if (this.uploadStrategy === 'loop') return '';

      var groups = isParent
        ? [['父块依据', collectRules(this.refs.parentRules)], ['子块依据', collectRules(this.refs.rules)]]
        : [['分片依据', collectRules(this.refs.rules)]];

      for (var g = 0; g < groups.length; g++) {
        var label = groups[g][0];
        var rows = groups[g][1];
        if (!rows.length) return label + '至少要有一条';
        for (var i = 0; i < rows.length; i++) {
          var rule = rows[i];
          if (rule.type === 'separator' && !String(rule.value).trim()) {
            return label + '第 ' + (i + 1) + ' 条：分隔符不能为空';
          }
          if (rule.type === 'length') {
            var length = Number(String(rule.value).trim());
            if (!length || length < 50 || length > 4000) {
              return label + '第 ' + (i + 1) + ' 条：固定长度需在 50~4000 之间';
            }
          }
        }
      }
      return '';
    }

    async submitUpload() {
      var file = this.pendingFile;
      if (!file) return this.notify('请先选择文件', 'warning');
      if (this.isStreaming) return this.notify('请等待当前操作完成', 'warning');

      var invalid = this.validateUploadForm();
      if (invalid) return this.notify(invalid, 'error');

      var isParent = this.uploadStrategy === 'parent_child';
      var formData = new FormData();
      formData.append('file', file);
      formData.append('strategy', this.uploadStrategy || 'general');

      // 分片依据以规则列表提交；不再传旧的 separator（避免「同时传了两者、后者被静默忽略」）
      if (isParent) {
        formData.append('parent_rules', JSON.stringify(collectRules(this.refs.parentRules)));
        formData.append('rules', JSON.stringify(collectRules(this.refs.rules)));
      } else if (this.uploadStrategy !== 'loop') {
        formData.append('rules', JSON.stringify(collectRules(this.refs.rules)));
      }

      var size = (this.refs.chunkSizeInput && this.refs.chunkSizeInput.value || '').trim();
      if (size) formData.append('chunk_size', size);
      if (isParent) {
        var parent = (this.refs.parentSizeInput && this.refs.parentSizeInput.value || '').trim();
        if (parent) formData.append('parent_size', parent);
      } else {
        var overlap = (this.refs.overlapInput && this.refs.overlapInput.value || '').trim();
        if (overlap) formData.append('chunk_overlap', overlap);
      }

      this.isStreaming = true;
      this.updateSendState();
      this.renderPendingFile();
      this.showUploadOverlay(true, file.name);

      try {
        var res = await fetch(API + '/upload', { method: 'POST', body: formData });

        var payload = null;
        try { payload = await res.json(); } catch (err) { payload = null; }

        if (!res.ok) {
          var detail = payload && (payload.detail || payload.message);
          throw new Error(detail || ('HTTP ' + res.status));
        }

        var data = (payload && payload.data) || {};
        this.uploadChunks = data.chunks || [];
        this.uploadMeta = {
          total: data.total || 0,
          truncated: !!data.truncated,
          stats: data.stats || {},
          filename: data.filename || file.name,
          indexed: data.indexed !== false
        };
        this.renderChunkPreview();

        if (this.uploadMeta.indexed) {
          this.notify('已上传，本次切出 ' + this.uploadMeta.total + ' 块', 'success');
        } else {
          this.notify('文件已保存，但建立索引失败（详见服务端日志）', 'error');
        }
        this.clearUploadFile();
      } catch (e) {
        console.error('文件上传失败:', e);
        this.notify('上传失败: ' + e.message, 'error');
      } finally {
        this.showUploadOverlay(false);
        this.isStreaming = false;
        this.updateSendState();
        this.renderPendingFile();
      }
    }

    /* chunk 预览：只渲染内存里的 this.uploadChunks，不落库、不进历史、刷新即失 */
    renderChunkPreview() {
      var box = this.refs.uploadPreview;
      if (!box) return;

      var chunks = this.uploadChunks || [];
      if (!chunks.length) {
        box.innerHTML = '';
        return;
      }

      var meta = this.uploadMeta || {};
      var stats = meta.stats || {};
      var head =
        '<div class="upload-preview__head">' +
        '<p class="sidebar__label">本次分片结果</p>' +
        '<p class="upload-preview__stat">共 ' + meta.total + ' 块 · 平均 ' +
        escapeHtml(stats.avg_chars === undefined || stats.avg_chars === null ? '-' : stats.avg_chars) +
        ' 字符' +
        (stats.parent_count ? ' · 父块 ' + escapeHtml(stats.parent_count) + ' 个' : '') +
        (meta.truncated ? ' · 仅显示前 ' + chunks.length + ' 块' : '') +
        '</p></div>';

      var cards = chunks.map(function (c) {
        var text = c.content || '';
        var preview = text.slice(0, 200);
        var more = text.length > preview.length;
        return '<article class="chunk-card stagger">' +
          '<div class="chunk-card__head">' +
          '<span class="chunk-card__idx">#' + (c.index + 1) + '</span>' +
          '<span class="badge badge--strong">' + escapeHtml(c.chunk_id || '') + '</span>' +
          (c.parent_id ? '<span class="badge">父 ' + escapeHtml(c.parent_id) + '</span>' : '') +
          '<span class="chunk-card__meta">' + escapeHtml(c.chars) + ' 字符</span>' +
          '</div>' +
          '<pre class="chunk-card__body">' + escapeHtml(preview) + (more ? '…' : '') + '</pre>' +
          '<pre class="chunk-card__full">' + escapeHtml(text) + '</pre>' +
          (more ? '<button class="btn btn--ghost btn--sm chunk-card__toggle" type="button">展开全文</button>' : '') +
          (c.cut ? '<p class="chunk-card__note">单块最多回传 2000 字符</p>' : '') +
          '</article>';
      }).join('');

      box.innerHTML = head + cards;
    }

    showUploadOverlay(show, fileName) {
      var overlay = this.refs.overlay;
      if (!overlay) return;
      overlay.classList.toggle('is-open', !!show);
      if (show) {
        var text = overlay.querySelector('.loading-text');
        var sub = overlay.querySelector('.loading-subtext');
        if (text) text.textContent = '正在上传并建立索引…';
        if (sub) sub.textContent = fileName ? ('上传: ' + fileName) : '';
      }
    }

    /* ----------------------------- AI Ops ----------------------------- */

    async triggerAIOps() {
      if (this.isStreaming) return this.notify('请等待当前操作完成', 'warning');

      this.newChat();
      var holder = this.addLoadingMessage('正在分析…');
      this.isStreaming = true;
      this.updateSendState();

      try {
        await this.runAIOps(holder);
      } catch (e) {
        console.error('AI Ops 分析失败:', e);
        var body = holder.querySelector('.msg__content');
        if (body) body.textContent = '抱歉，AI Ops 分析时出现错误：' + e.message;
        holder.classList.add('msg--error');
      } finally {
        this.isStreaming = false;
        this.updateSendState();
        this.saveCurrentChat();
        this.renderHistories();
      }
    }

    async runAIOps(holder) {
      var res = await fetch(API + '/aiops', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: this.sessionId })
      });
      if (!res.ok) throw new Error('HTTP 错误: ' + res.status);

      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      var full = '';
      var self = this;

      // 后端可能把多条 JSON 塞进同一行，先做一次批量抽取
      var MULTI_JSON = /\{"type"\s*:\s*"[^"]+"\s*,\s*"data"\s*:\s*(?:"[^"]*"|null)\}/g;

      function consume(msg) {
        if (!msg || typeof msg.type !== 'string') return false;
        switch (msg.type) {
          case 'content':
            full += msg.data || '';
            return false;
          case 'plan':
            full += '\n\n### 执行计划\n\n' + (msg.message || '') + '\n\n';
            return false;
          case 'step_complete':
            full += '\n- ' + (msg.message || '') + '\n';
            return false;
          case 'status':
            full += '\n' + (msg.message || '') + '\n';
            return false;
          case 'report':
            full += '\n\n### 诊断报告\n\n' + (msg.report || '') + '\n';
            return false;
          case 'complete':
            if (msg.response) full += '\n\n' + msg.response;
            return true;
          case 'done':
            return true;
          case 'error':
            throw new Error(msg.data || msg.message || 'AI Ops 分析失败');
          default:
            return false;
        }
      }

      try {
        while (true) {
          var step = await reader.read();
          if (step.done) break;

          buffer += decoder.decode(step.value, { stream: true });
          var lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (!line.trim()) continue;
            if (line.startsWith('id:') || line.startsWith('event:')) continue;
            if (!line.startsWith('data:')) continue;

            var raw = line.slice(5).trim();

            var matched = raw.match(MULTI_JSON);
            if (matched && matched.length) {
              var finished = false;
              matched.forEach(function (piece) {
                var parsed = null;
                try { parsed = JSON.parse(piece); } catch (e) { parsed = null; }
                if (parsed && consume(parsed)) finished = true;
              });
              self.paintStream(holder, full);
              if (finished) { self.finishStream(holder, full); return; }
              continue;
            }

            var msg = null;
            try { msg = JSON.parse(raw); } catch (e) { msg = null; }

            if (msg && typeof msg.type === 'string') {
              var done = consume(msg);
              self.paintStream(holder, full);
              if (done) { self.finishStream(holder, full); return; }
            } else {
              full += raw;
              self.paintStream(holder, full);
            }
          }
        }

        if (full) this.finishStream(holder, full);
      } finally {
        reader.releaseLock();
      }
    }

    /* ------------------------------ 提示 ------------------------------ */

    notify(message, type) {
      var wrap = this.refs.toasts;
      if (!wrap) return;
      var node = document.createElement('div');
      node.className = 'toast' + (type === 'error' ? ' toast--error' : '');
      node.textContent = message;
      wrap.appendChild(node);
      setTimeout(function () { node.remove(); }, 3200);
    }
  }

  /* ========================================================================
     系统设置抽屉
     ====================================================================== */

  class ConfigDrawer {
    constructor() {
      this.drawer = el('cfgDrawer');
      this.body = el('cfgBody');
      if (!this.drawer || !this.body) return;

      var on = (node, type, fn) => { if (node) node.addEventListener(type, fn); };
      on(el('configBtn'), 'click', () => this.toggle());
      on(el('cfgCloseBtn'), 'click', () => this.close());
      on(el('cfgRefreshBtn'), 'click', () => this.load());

      var self = this;
      this.drawer.addEventListener('click', function (e) {
        if (e.target === self.drawer) self.close();
      });
    }

    /* 侧栏入口当开关用：已打开时再点一次就收起 */
    toggle() {
      if (this.drawer.classList.contains('is-open')) {
        this.close();
        return;
      }
      this.open();
    }

    open() {
      closeOtherPanels(this.drawer);   // 与上传附件抽屉互斥
      this.drawer.classList.add('is-open');
      this.drawer.setAttribute('aria-hidden', 'false');
      this.load();
    }

    close() {
      this.drawer.classList.remove('is-open');
      this.drawer.setAttribute('aria-hidden', 'true');
    }

    badge(text, strong) {
      return '<span class="badge' + (strong ? ' badge--strong' : '') + '">' + escapeHtml(text) + '</span>';
    }

    channel(c) {
      if (!c || !c.configured) return this.badge('未配置', false);
      var html = this.badge('已配置', true) + (c.signed ? ' ' + this.badge('加签', false) : '');
      if (c.webhook_masked) html += '<span class="mono">' + escapeHtml(c.webhook_masked) + '</span>';
      return html;
    }

    group(title, rows) {
      var body = rows.map(function (r) {
        return '<tr><th>' + escapeHtml(r[0]) + '</th><td>' + r[1] + '</td></tr>';
      }).join('');
      return '<section class="cfg__group stagger"><h4>' + escapeHtml(title) + '</h4>' +
        '<table class="cfg__table">' + body + '</table></section>';
    }

    async load() {
      this.body.innerHTML = '<p class="loading-text-block">正在加载配置…</p>';
      try {
        var res = await fetch(API + '/config');
        var payload = await res.json();
        if (!res.ok || payload.code >= 400) throw new Error(payload.message || ('HTTP ' + res.status));
        var d = payload.data;

        this.body.innerHTML =
          this.group('应用信息', [
            ['应用名称', escapeHtml(d.app.name)],
            ['版本', escapeHtml(d.app.version)],
            ['运行模式', d.app.debug ? this.badge('开发 (DEBUG)', true) : this.badge('生产', false)],
            ['通知卡片地址', escapeHtml(d.app.app_url)],
            ['Python', escapeHtml(d.app.python)],
            ['操作系统', escapeHtml(d.app.platform)]
          ]) +
          this.group('向量数据库', [
            ['类型', escapeHtml(d.vector_db.type)],
            ['地址', escapeHtml(d.vector_db.host + ':' + d.vector_db.port)],
            ['连接状态', d.vector_db.connected ? this.badge('已连接', true) : this.badge('未连接', false)],
            ['知识库 collection', escapeHtml(d.vector_db.knowledge_collection)],
            ['主 collection', escapeHtml(d.vector_db.primary_collection)],
            ['向量维度', escapeHtml(d.vector_db.vector_dim)],
            ['调用超时', escapeHtml(d.vector_db.timeout_ms + ' ms')]
          ]) +
          this.group('RAG 检索', [
            ['单路召回条数', escapeHtml(d.rag.top_k)],
            ['多路检索', d.rag.multi_query_enabled ? this.badge('已开启', true) : this.badge('已关闭', false)],
            ['改写条数', escapeHtml(d.rag.rewrite_count)],
            ['实际检索路数', escapeHtml(d.rag.retrieval_paths + ' 路（原问题 + 改写）')],
            ['去重键', escapeHtml(d.rag.dedup_key)],
            ['切片大小', escapeHtml(d.rag.chunk_max_size + '（二次切分 ' + d.rag.chunk_max_size * 2 + '）')],
            ['切片重叠', escapeHtml(d.rag.chunk_overlap)]
          ]) +
          this.group('模型', [
            ['提供方', escapeHtml(d.model.provider)],
            ['对话模型', escapeHtml(d.model.chat_model)],
            ['嵌入模型', escapeHtml(d.model.embedding_model)],
            ['视觉模型', escapeHtml(d.model.vision_model)],
            ['API Key', escapeHtml(d.model.api_key_masked)]
          ]) +
          this.group('通知渠道', [
            ['钉钉', this.channel(d.notify.dingtalk)],
            ['飞书', this.channel(d.notify.feishu)],
            ['企业微信', this.channel(d.notify.wecom)],
            ['webhook 去重窗口', escapeHtml(d.notify.webhook_dedup_window + ' 秒')]
          ]) +
          this.group('外部集成', [
            ['Prometheus', escapeHtml(d.integration.prometheus_base_url)],
            ['Prometheus 超时', escapeHtml(d.integration.prometheus_timeout + ' 秒')],
            ['CLS MCP', escapeHtml(d.integration.mcp_cls_url + '（' + d.integration.mcp_cls_transport + '）')],
            ['Monitor MCP', escapeHtml(d.integration.mcp_monitor_url + '（' + d.integration.mcp_monitor_transport + '）')]
          ]);
      } catch (e) {
        this.body.innerHTML = '<p class="error-block">配置加载失败：' + escapeHtml(e.message) + '</p>';
      }
    }
  }

  /* ============================== 启动 ============================== */

  document.addEventListener('DOMContentLoaded', function () {
    window.app = new App();
    window.configDrawer = new ConfigDrawer();
  });
})();
