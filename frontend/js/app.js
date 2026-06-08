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
        <div class="item-card-head">
          <div class="item-title">${escapeHtml(skill.name)}</div>
          <button class="icon-action" type="button" title="编辑 SKILL.md" aria-label="编辑 ${escapeHtml(skill.name)}">编辑</button>
        </div>
        <div class="item-meta">${escapeHtml(skill.description || '未填写描述')}</div>
      `;
      item.querySelector('.icon-action').addEventListener('click', () => openSkillEditor(skill.name));
      return item;
    });
  } catch (err) {
    listEl.innerHTML = `<div class="msg error">Skill 加载失败：${escapeHtml(err.message)}</div>`;
  }
}

async function openSkillEditor(skillName) {
  const dialog = document.getElementById('skillEditDialog');
  const title = document.getElementById('skillEditTitle');
  const meta = document.getElementById('skillEditMeta');
  const editor = document.getElementById('skillEditor');
  const status = document.getElementById('skillEditStatus');
  const saveBtn = document.getElementById('saveSkillBtn');
  if (!dialog || !editor) return;

  title.textContent = `编辑 ${skillName}`;
  meta.textContent = '正在读取 SKILL.md...';
  status.textContent = '';
  editor.value = '';
  saveBtn.disabled = true;
  dialog.dataset.skillName = skillName;
  dialog.showModal();

  try {
    const resp = await fetch(`/api/skills/${encodeURIComponent(skillName)}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '读取失败');
    editor.value = data.content || '';
    meta.textContent = data.path || '';
    saveBtn.disabled = false;
    editor.focus();
  } catch (err) {
    status.textContent = '读取失败：' + err.message;
  }
}

async function loadKnowledge() {
  const listEl = document.getElementById('knowledgeList');
  if (!listEl) return;
  try {
    const [statusResp, filesResp] = await Promise.all([
      fetch('/api/bailian/knowledge/status'),
      fetch('/api/bailian/files'),
    ]);
    const status = await statusResp.json();
    const filesData = await filesResp.json();
    if (!statusResp.ok) throw new Error(status.detail || '状态加载失败');
    if (!filesResp.ok) throw new Error(filesData.detail || '文件加载失败');
    renderKnowledgeStatus(status, filesData.files || []);
    renderRemoteFiles(listEl, filesData.files || []);
  } catch (err) {
    listEl.innerHTML = `<div class="msg error">远程知识库加载失败：${escapeHtml(err.message)}</div>`;
  }
}

function renderKnowledgeStatus(status, files) {
  const workspaceEl = document.getElementById('bailianWorkspace');
  const indexEl = document.getElementById('bailianIndex');
  const countEl = document.getElementById('bailianFileCount');
  const ragEl = document.getElementById('bailianRagStatus');
  const categoryEl = document.getElementById('bailianCategory');
  const categoryInput = document.getElementById('indexCategoryInput');
  const indexNameInput = document.getElementById('indexNameInput');
  const uploadCategoryInput = document.getElementById('knowledgeCategoryInput');
  if (workspaceEl) workspaceEl.textContent = status.workspace_id || '-';
  if (indexEl) indexEl.textContent = status.index_id || '-';
  if (countEl) countEl.textContent = `${files.length} 个`;
  if (ragEl) ragEl.textContent = status.rag_enabled ? '已启用' : '未启用';
  if (categoryEl) categoryEl.textContent = status.category_name || 'plan-generator-ecs';
  if (categoryInput && !categoryInput.value) categoryInput.value = status.category_name || '';
  if (indexNameInput && !indexNameInput.value) indexNameInput.value = nextIndexName(status.index_name || 'pg-ecs-v4');
  if (uploadCategoryInput && !uploadCategoryInput.value) uploadCategoryInput.value = status.category_name || '';
}

function nextIndexName(current) {
  const match = String(current || '').match(/^(.*?)(\d+)$/);
  if (!match) return 'pg-ecs-v5';
  const prefix = match[1];
  const next = String(Number(match[2]) + 1);
  return `${prefix}${next}`.slice(0, 20);
}

function renderRemoteFiles(container, files) {
  container.innerHTML = '';
  if (!files.length) {
    container.innerHTML = '<div class="item-card"><div class="item-title">远程类目中暂无文件</div></div>';
    return;
  }
  files.forEach((file) => {
    const item = document.createElement('div');
    item.className = 'remote-file-row';
    item.innerHTML = `
      <div>
        <div class="item-title">${escapeHtml(file.file_name || file.file_id)}</div>
        <div class="item-meta">
          ${escapeHtml(file.status || '-')}&nbsp;&nbsp;${escapeHtml(file.file_type || '-')}&nbsp;&nbsp;${formatKb(file.size_in_bytes)}
        </div>
      </div>
      <button class="danger ghost" type="button" data-file-id="${escapeHtml(file.file_id)}">删除</button>
    `;
    item.querySelector('button').addEventListener('click', () => deleteRemoteFile(file.file_id));
    container.appendChild(item);
  });
}

function formatKb(size) {
  const value = Number(size || 0);
  if (!value) return '-';
  return `${Math.ceil(value / 1024)} KB`;
}

async function deleteRemoteFile(fileId) {
  if (!fileId) return;
  const statusEl = document.getElementById('knowledgeUploadStatus');
  if (!confirm('确认删除该百炼远程文件？删除后需要重新创建索引才会影响新知识库。')) return;
  statusEl.textContent = '正在删除远程文件...';
  try {
    const resp = await fetch(`/api/bailian/files/${encodeURIComponent(fileId)}`, { method: 'DELETE' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '删除失败');
    statusEl.textContent = '远程文件已删除';
    await loadKnowledge();
  } catch (err) {
    statusEl.textContent = '删除失败：' + err.message;
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
    if (event === 'trace') {
      addMessage(messagesEl, 'trace', data.message || 'Agent 执行中...');
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
  const editForm = document.getElementById('skillEditForm');
  const uploadStatus = document.getElementById('uploadStatus');
  const uploadDialog = document.getElementById('skillUploadDialog');
  const editDialog = document.getElementById('skillEditDialog');
  const openUploadBtn = document.getElementById('openSkillUploadBtn');
  const skillNameInput = document.getElementById('skillNameInput');
  const skillFileInput = document.getElementById('skillFileInput');
  const skillEditor = document.getElementById('skillEditor');
  const skillEditStatus = document.getElementById('skillEditStatus');
  const saveSkillBtn = document.getElementById('saveSkillBtn');
  loadSkills();
  openUploadBtn.addEventListener('click', () => uploadDialog.showModal());
  uploadForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (event.submitter?.value === 'cancel') {
      uploadDialog.close();
      return;
    }
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
      uploadDialog.close();
      await loadSkills();
    } catch (err) {
      uploadStatus.textContent = '上传失败：' + err.message;
    }
  });

  editForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (event.submitter?.value === 'cancel') {
      editDialog.close();
      return;
    }
    const skillName = editDialog.dataset.skillName;
    if (!skillName) return;
    const content = skillEditor.value.trim();
    if (!content) {
      skillEditStatus.textContent = 'Skill 内容不能为空';
      return;
    }
    saveSkillBtn.disabled = true;
    skillEditStatus.textContent = '正在保存...';
    try {
      const resp = await fetch(`/api/skills/${encodeURIComponent(skillName)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '保存失败');
      skillEditStatus.textContent = '保存完成，AgentScope Skill 已刷新';
      editDialog.close();
      await loadSkills();
    } catch (err) {
      skillEditStatus.textContent = '保存失败：' + err.message;
    } finally {
      saveSkillBtn.disabled = false;
    }
  });
}

function initKnowledgePage() {
  const form = document.getElementById('knowledgeUploadForm');
  const uploadDialog = document.getElementById('knowledgeUploadDialog');
  const openUploadBtn = document.getElementById('openKnowledgeUploadBtn');
  const categoryInput = document.getElementById('knowledgeCategoryInput');
  const fileInput = document.getElementById('knowledgeFileInput');
  const statusEl = document.getElementById('knowledgeUploadStatus');
  const refreshBtn = document.getElementById('refreshKnowledgeBtn');
  const createIndexBtn = document.getElementById('createIndexBtn');
  const checkIndexJobBtn = document.getElementById('checkIndexJobBtn');
  const indexCategoryInput = document.getElementById('indexCategoryInput');
  const indexNameInput = document.getElementById('indexNameInput');
  const indexJobStatus = document.getElementById('indexJobStatus');
  const retrieveQueryInput = document.getElementById('retrieveQueryInput');
  const runRetrieveBtn = document.getElementById('runRetrieveBtn');
  const retrieveResults = document.getElementById('retrieveResults');
  loadKnowledge();
  openUploadBtn.addEventListener('click', () => uploadDialog.showModal());
  refreshBtn.addEventListener('click', loadKnowledge);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (event.submitter?.value === 'cancel') {
      uploadDialog.close();
      return;
    }
    const file = fileInput.files[0];
    if (!file) {
      statusEl.textContent = '请选择 md、txt、docx 或 pdf 文件';
      return;
    }
    const formData = new FormData();
    formData.append('file', file);
    formData.append('category_name', categoryInput.value.trim() || 'plan-generator-ecs');
    statusEl.textContent = '正在上传到百炼...';
    try {
      const resp = await fetch('/api/bailian/files/upload', { method: 'POST', body: formData });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '上传失败');
      statusEl.textContent = '上传完成。请创建新索引后用于方案生成。';
      fileInput.value = '';
      uploadDialog.close();
      await loadKnowledge();
    } catch (err) {
      statusEl.textContent = '上传失败：' + err.message;
    }
  });

  createIndexBtn.addEventListener('click', async () => {
    createIndexBtn.disabled = true;
    indexJobStatus.textContent = '正在创建百炼远程索引...';
    try {
      const resp = await fetch('/api/bailian/index/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          category_name: indexCategoryInput.value.trim(),
          index_name: indexNameInput.value.trim(),
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '创建失败');
      indexJobStatus.textContent = `索引已创建：${data.index_id}，任务：${data.job_id || '-'}`;
      await loadKnowledge();
    } catch (err) {
      indexJobStatus.textContent = '创建索引失败：' + err.message;
    } finally {
      createIndexBtn.disabled = false;
    }
  });

  checkIndexJobBtn.addEventListener('click', async () => {
    indexJobStatus.textContent = '正在查询索引任务...';
    try {
      const resp = await fetch('/api/bailian/index/job');
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '查询失败');
      indexJobStatus.textContent = `任务 ${data.job_id || '-'}：${data.status || '-'}`;
    } catch (err) {
      indexJobStatus.textContent = '查询失败：' + err.message;
    }
  });

  runRetrieveBtn.addEventListener('click', async () => {
    const query = retrieveQueryInput.value.trim();
    if (!query) {
      retrieveResults.innerHTML = '<div class="msg error">请输入检索问题</div>';
      return;
    }
    runRetrieveBtn.disabled = true;
    retrieveResults.innerHTML = '<div class="item-card"><div class="item-title">正在检索百炼知识库...</div></div>';
    try {
      const resp = await fetch(`/api/bailian/retrieve?query=${encodeURIComponent(query)}`);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '检索失败');
      renderRetrieveResults(retrieveResults, data.nodes || []);
    } catch (err) {
      retrieveResults.innerHTML = `<div class="msg error">检索失败：${escapeHtml(err.message)}</div>`;
    } finally {
      runRetrieveBtn.disabled = false;
    }
  });
}

function renderRetrieveResults(container, nodes) {
  container.innerHTML = '';
  if (!nodes.length) {
    container.innerHTML = '<div class="item-card"><div class="item-title">没有命中结果</div></div>';
    return;
  }
  nodes.forEach((node, index) => {
    const item = document.createElement('div');
    item.className = 'retrieve-card';
    item.innerHTML = `
      <div class="item-card-head">
        <div class="item-title">片段 ${index + 1}</div>
        <span class="status-pill">score ${Number(node.score || 0).toFixed(3)}</span>
      </div>
      <pre>${escapeHtml(node.text || '')}</pre>
    `;
    container.appendChild(item);
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
