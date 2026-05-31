const pageHost = document.getElementById('pageHost');
const pageTitle = document.getElementById('pageTitle');
const pageEyebrow = document.getElementById('pageEyebrow');
const navItems = [...document.querySelectorAll('.nav-item')];
const resetBtn = document.getElementById('resetBtn');
const refreshAllBtn = document.getElementById('refreshAllBtn');

const pageMeta = {
  plan: { title: '方案生成', eyebrow: 'Conversation' },
  skills: { title: 'Skill 管理', eyebrow: 'AgentScope' },
  knowledge: { title: '知识库', eyebrow: 'RAG' },
  'page-agent': { title: 'Page Agent', eyebrow: 'MCP' },
};

let sessionId = localStorage.getItem('planGeneratorSessionId') || null;
let currentPage = 'plan';
let messagesEl = null;
let pageAgentMessagesEl = null;

function scrollToBottom(el) {
  if (el) el.scrollTop = el.scrollHeight;
}

function addMessage(container, role, text, extraHtml = '') {
  if (!container) return null;
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  if (extraHtml) {
    const wrap = document.createElement('div');
    wrap.innerHTML = extraHtml;
    div.appendChild(wrap);
  }
  container.appendChild(div);
  scrollToBottom(container);
  return div;
}

async function typeInto(el, text) {
  el.textContent = '';
  const step = Math.max(1, Math.ceil(text.length / 90));
  for (let i = 0; i < text.length; i += step) {
    el.textContent += text.slice(i, i + step);
    scrollToBottom(el.parentElement);
    await new Promise((resolve) => setTimeout(resolve, 8));
  }
}

function setActiveNav(page) {
  navItems.forEach((item) => item.classList.toggle('active', item.dataset.page === page));
}

async function loadPage(page) {
  currentPage = page;
  setActiveNav(page);
  pageTitle.textContent = pageMeta[page].title;
  pageEyebrow.textContent = pageMeta[page].eyebrow;
  const resp = await fetch(`/pages/${page}.html`);
  pageHost.innerHTML = await resp.text();

  if (page === 'plan') initPlanPage();
  if (page === 'skills') initSkillsPage();
  if (page === 'knowledge') initKnowledgePage();
  if (page === 'page-agent') initPageAgentPage();
}

function renderItemGrid(container, items, emptyText, render) {
  container.innerHTML = '';
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'item-card';
    empty.innerHTML = `<div class="item-title">${emptyText}</div>`;
    container.appendChild(empty);
    return;
  }
  items.forEach((item) => container.appendChild(render(item)));
}

async function loadSkills() {
  const listEl = document.getElementById('skillList');
  const countEl = document.getElementById('skillCount');
  if (!listEl) return;
  try {
    const resp = await fetch('/api/skills');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '加载失败');
    const skills = data.skills || [];
    if (countEl) countEl.textContent = `${skills.length} 个`;
    renderItemGrid(listEl, skills, '暂无 Skill', (skill) => {
      const item = document.createElement('div');
      item.className = 'item-card';
      item.innerHTML = `
        <div class="item-title">${escapeHtml(skill.name)}</div>
        <div class="item-meta">${escapeHtml(skill.description || '未填写描述')}</div>
      `;
      return item;
    });
  } catch (err) {
    listEl.innerHTML = `<div class="msg error">Skill 加载失败：${escapeHtml(err.message)}</div>`;
  }
}

async function loadKnowledge() {
  const listEl = document.getElementById('knowledgeList');
  if (!listEl) return;
  try {
    const resp = await fetch('/api/knowledge');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '加载失败');
    renderItemGrid(listEl, data.documents || [], '暂无知识文档', (doc) => {
      const item = document.createElement('div');
      item.className = 'item-card';
      item.innerHTML = `
        <div class="item-title">${escapeHtml(doc.name)}</div>
        <div class="item-meta">${escapeHtml(doc.path)}<br>${Math.ceil(doc.size / 1024)} KB</div>
      `;
      return item;
    });
  } catch (err) {
    listEl.innerHTML = `<div class="msg error">知识文档加载失败：${escapeHtml(err.message)}</div>`;
  }
}

function initPlanPage() {
  messagesEl = document.getElementById('messages');
  const inputEl = document.getElementById('messageInput');
  const sendBtn = document.getElementById('sendBtn');
  const executeValidationInput = document.getElementById('executeValidationInput');
  addMessage(messagesEl, 'assistant', '请粘贴检修需求描述或需求文档。我会逐步抽取信息、追问缺失项，并在信息齐全后生成 Word 检修方案。');

  const setBusy = (busy) => {
    sendBtn.disabled = busy;
    inputEl.disabled = busy;
    sendBtn.textContent = busy ? '生成中...' : '发送';
  };

  async function sendMessage() {
    const text = inputEl.value.trim();
    if (!text) return;
    addMessage(messagesEl, 'user', text);
    inputEl.value = '';
    setBusy(true);
    try {
      const resp = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          message: text,
          execute_validation: executeValidationInput.checked,
        }),
      });
      if (!resp.ok || !resp.body) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || '请求失败');
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        buffer = parseSseChunk(buffer, handlePlanEvent);
      }
      if (buffer.trim()) parseSseChunk(buffer + '\n\n', handlePlanEvent);
    } catch (err) {
      addMessage(messagesEl, 'error', '处理失败：' + err.message);
    } finally {
      setBusy(false);
      inputEl.focus();
    }
  }

  async function handlePlanEvent(event, data) {
    if (data.session_id) {
      sessionId = data.session_id;
      localStorage.setItem('planGeneratorSessionId', sessionId);
    }
    if (event === 'status' || event === 'collected') {
      addMessage(messagesEl, 'status', data.message || '处理中...');
      return;
    }
    if (event === 'evidence') {
      const skills = (data.selected_skills || []).join('、') || '未命中明确 Skill';
      const ragText = data.rag_enabled
        ? `RAG 已启用，命中 ${data.rag_chunks_count || 0} 个参考片段`
        : 'RAG 未启用，当前仅使用 Skill 规则与用户输入';
      addMessage(messagesEl, 'status', `${data.message || '生成依据已准备完成'}\n候选 Skill：${skills}\n${ragText}`);
      return;
    }
    if (event === 'done') {
      if (data.status === 'generated') {
        const validationText = data.validation_result ? `\n\nPage Agent 验证结果：\n${data.validation_result}` : '';
        const msg = addMessage(messagesEl, 'assistant', '');
        await typeInto(msg, data.message + validationText);
        const wrap = document.createElement('div');
        wrap.innerHTML = `<a class="download" href="${data.download_url}">下载检修方案</a>`;
        msg.appendChild(wrap);
      } else {
        const msg = addMessage(messagesEl, 'assistant', '');
        await typeInto(msg, data.message || '');
      }
      return;
    }
    if (event === 'error') addMessage(messagesEl, 'error', data.message || '处理失败');
  }

  sendBtn.addEventListener('click', sendMessage);
  inputEl.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      sendMessage();
    }
  });
}

function initSkillsPage() {
  const uploadForm = document.getElementById('uploadForm');
  const uploadStatus = document.getElementById('uploadStatus');
  const skillNameInput = document.getElementById('skillNameInput');
  const skillFileInput = document.getElementById('skillFileInput');
  loadSkills();
  uploadForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const file = skillFileInput.files[0];
    if (!file) {
      uploadStatus.textContent = '请选择 SKILL.md 或 zip 文件';
      return;
    }
    const formData = new FormData();
    formData.append('file', file);
    formData.append('skill_name', skillNameInput.value.trim());
    uploadStatus.textContent = '正在上传...';
    try {
      const resp = await fetch('/api/skills/upload', { method: 'POST', body: formData });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '上传失败');
      uploadStatus.textContent = '上传完成，Skill 已刷新';
      skillFileInput.value = '';
      skillNameInput.value = '';
      await loadSkills();
    } catch (err) {
      uploadStatus.textContent = '上传失败：' + err.message;
    }
  });
}

function initKnowledgePage() {
  const form = document.getElementById('knowledgeUploadForm');
  const categoryInput = document.getElementById('knowledgeCategoryInput');
  const fileInput = document.getElementById('knowledgeFileInput');
  const statusEl = document.getElementById('knowledgeUploadStatus');
  const reindexBtn = document.getElementById('reindexBtn');
  loadKnowledge();

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const file = fileInput.files[0];
    if (!file) {
      statusEl.textContent = '请选择 md、txt 或 docx 文件';
      return;
    }
    const formData = new FormData();
    formData.append('file', file);
    formData.append('category', categoryInput.value.trim() || 'uploaded');
    statusEl.textContent = '正在导入...';
    try {
      const resp = await fetch('/api/knowledge/upload', { method: 'POST', body: formData });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '导入失败');
      statusEl.textContent = '导入完成，下一次检索会重建索引';
      fileInput.value = '';
      await loadKnowledge();
    } catch (err) {
      statusEl.textContent = '导入失败：' + err.message;
    }
  });

  reindexBtn.addEventListener('click', async () => {
    reindexBtn.disabled = true;
    statusEl.textContent = '正在重建索引...';
    try {
      const resp = await fetch('/api/rag/reindex', { method: 'POST' });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '重建失败');
      statusEl.textContent = data.status === 'ok' ? '索引重建完成' : data.message || 'RAG 未启用';
    } catch (err) {
      statusEl.textContent = '索引重建失败：' + err.message;
    } finally {
      reindexBtn.disabled = false;
    }
  });
}

function initPageAgentPage() {
  pageAgentMessagesEl = document.getElementById('pageAgentMessages');
  const taskInput = document.getElementById('pageAgentTaskInput');
  const startBtn = document.getElementById('startMcpBtn');
  const runBtn = document.getElementById('runPageAgentBtn');
  const statusEl = document.getElementById('mcpStatus');
  addMessage(pageAgentMessagesEl, 'assistant', '这里用于单独测试 Page Agent。输出不会混入主方案生成对话。');

  startBtn.addEventListener('click', async () => {
    startBtn.disabled = true;
    statusEl.textContent = '正在启动 Page Agent MCP...';
    try {
      const resp = await fetch('/api/mcp/start', { method: 'POST' });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || data.message || '启动失败');
      statusEl.textContent = 'Page Agent 已启动。请在 Chrome 中确认扩展 Hub 已连接。';
    } catch (err) {
      statusEl.textContent = 'Page Agent 启动失败：' + err.message;
    } finally {
      startBtn.disabled = false;
    }
  });

  runBtn.addEventListener('click', async () => {
    const task = taskInput.value.trim();
    if (!task) {
      statusEl.textContent = '请输入一条 Page Agent 测试指令';
      return;
    }
    addMessage(pageAgentMessagesEl, 'user', task);
    runBtn.disabled = true;
    statusEl.textContent = 'Page Agent 正在执行测试指令...';
    try {
      const resp = await fetch('/api/page-agent/task', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || data.message || '执行失败');
      statusEl.textContent = 'Page Agent 执行完成';
      const msg = addMessage(pageAgentMessagesEl, 'assistant', '');
      await typeInto(msg, data.result || '无返回内容');
    } catch (err) {
      statusEl.textContent = 'Page Agent 执行失败：' + err.message;
      addMessage(pageAgentMessagesEl, 'error', 'Page Agent 执行失败：' + err.message);
    } finally {
      runBtn.disabled = false;
    }
  });
}

function parseSseChunk(buffer, onEvent) {
  const parts = buffer.split('\n\n');
  const rest = parts.pop();
  parts.forEach((part) => {
    let event = 'message';
    let data = '';
    part.split('\n').forEach((line) => {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      if (line.startsWith('data:')) data += line.slice(5).trim();
    });
    if (!data) return;
    onEvent(event, JSON.parse(data));
  });
  return rest;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

async function resetChat() {
  if (sessionId) {
    await fetch('/api/chat/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    }).catch(() => {});
  }
  sessionId = null;
  localStorage.removeItem('planGeneratorSessionId');
  if (currentPage !== 'plan') {
    await loadPage('plan');
  } else {
    await loadPage('plan');
  }
}

navItems.forEach((item) => {
  item.addEventListener('click', () => loadPage(item.dataset.page));
});

resetBtn.addEventListener('click', resetChat);
refreshAllBtn.addEventListener('click', () => loadPage(currentPage));

loadPage('plan');
