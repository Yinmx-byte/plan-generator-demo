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

function bindUploadDropzone(dropzone, input, fileNameEl) {
  if (!dropzone || !input) return;
  const updateName = () => {
    if (fileNameEl) fileNameEl.textContent = input.files[0]?.name || '未选择文件';
  };
  input.addEventListener('change', updateName);
  ['dragenter', 'dragover'].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add('dragging');
    });
  });
  ['dragleave', 'drop'].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove('dragging');
    });
  });
  dropzone.addEventListener('drop', (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.files = transfer.files;
    updateName();
  });
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
        <div>
          <div class="item-title">${escapeHtml(skill.name)}</div>
        </div>
        <div class="item-meta">${escapeHtml(skill.description || '未填写描述')}</div>
        <div class="card-actions">
          <button class="icon-action" type="button" title="编辑 SKILL.md" aria-label="编辑 ${escapeHtml(skill.name)}">编辑</button>
        </div>
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
  const countEl = document.getElementById('bailianFileCount');
  const docCountEl = document.getElementById('knowledgeDocCount');
  const ragEl = document.getElementById('bailianRagStatus');
  const categoryEl = document.getElementById('bailianCategory');
  if (workspaceEl) workspaceEl.textContent = status.workspace_id || '-';
  if (countEl) countEl.textContent = `${files.length} 个`;
  if (docCountEl) docCountEl.textContent = `${files.length} 个`;
  if (ragEl) ragEl.textContent = status.rag_enabled ? '已启用' : '未启用';
  if (categoryEl) categoryEl.textContent = status.category_name || 'plan-generator-ecs';
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
      <div class="card-actions">
        <button class="ghost" type="button" data-action="view" data-file-id="${escapeHtml(file.file_id)}">查看</button>
        <button class="danger ghost" type="button" data-action="delete" data-file-id="${escapeHtml(file.file_id)}">删除</button>
      </div>
    `;
    item.querySelector('[data-action="view"]').addEventListener('click', () => openKnowledgeFile(file.file_id));
    item.querySelector('[data-action="delete"]').addEventListener('click', () => deleteRemoteFile(file.file_id));
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
  if (!confirm('确认删除该知识文档？删除成功后系统会自动重建索引。')) return;
  statusEl.textContent = '正在删除远程文件...';
  try {
    const resp = await fetch(`/api/bailian/files/${encodeURIComponent(fileId)}`, { method: 'DELETE' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '删除失败');
    statusEl.textContent = '文件已删除，正在自动重建索引...';
    await rebuildKnowledgeIndex('删除文档后');
    await loadKnowledge();
  } catch (err) {
    statusEl.textContent = '删除失败：' + err.message;
  }
}

async function rebuildKnowledgeIndex(reason) {
  const statusEl = document.getElementById('knowledgeUploadStatus');
  const resp = await fetch('/api/bailian/index/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || '重建索引失败');
  if (statusEl) {
    statusEl.textContent = `${reason}索引重建任务已提交：${data.job_id || '-'}，文档数：${data.document_count || 0}`;
  }
  return data;
}

async function openKnowledgeFile(fileId) {
  const dialog = document.getElementById('knowledgeViewDialog');
  const title = document.getElementById('knowledgeViewTitle');
  const meta = document.getElementById('knowledgeViewMeta');
  const detailEl = document.getElementById('knowledgeViewDetail');
  const previewEl = document.getElementById('knowledgeContentPreview');
  const indexEl = document.getElementById('knowledgeIndexDetail');
  if (!dialog || !fileId) return;
  title.textContent = '查看知识文档';
  meta.textContent = fileId;
  detailEl.innerHTML = '<div class="item-card"><div class="item-title">正在读取百炼远程文件...</div></div>';
  previewEl.textContent = '-';
  indexEl.textContent = '-';
  dialog.showModal();
  try {
    const resp = await fetch(`/api/bailian/files/${encodeURIComponent(fileId)}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '查看失败');
    const file = data.file || {};
    title.textContent = file.file_name || fileId;
    meta.textContent = data.message || '百炼远程文件详情';
    detailEl.innerHTML = [
      ['文件 ID', file.file_id],
      ['文件名', file.file_name],
      ['类型', file.file_type],
      ['大小', formatKb(file.size_in_bytes)],
      ['状态', file.status],
      ['解析器', file.parser],
      ['类目 ID', file.category_id],
      ['创建时间', file.create_time],
      ['标签', (file.tags || []).join('，')],
      ['解析结果地址', file.parse_result_download_url],
    ].map(([label, value]) => `
      <div class="detail-item">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(value || '-')}</strong>
      </div>
    `).join('');
    previewEl.textContent = file.content_preview || '百炼当前未返回该文件的解析文本预览。';
    indexEl.textContent = data.index_document
      ? JSON.stringify(data.index_document, null, 2)
      : '当前索引中未找到该文件，可能需要重建索引。';
  } catch (err) {
    detailEl.innerHTML = `<div class="msg error">查看失败：${escapeHtml(err.message)}</div>`;
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
  const skillDropzone = document.getElementById('skillDropzone');
  const skillFileName = document.getElementById('skillFileName');
  const skillEditor = document.getElementById('skillEditor');
  const skillEditStatus = document.getElementById('skillEditStatus');
  const saveSkillBtn = document.getElementById('saveSkillBtn');
  loadSkills();
  bindUploadDropzone(skillDropzone, skillFileInput, skillFileName);
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
      if (skillFileName) skillFileName.textContent = '未选择文件';
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
  const fileInput = document.getElementById('knowledgeFileInput');
  const knowledgeDropzone = document.getElementById('knowledgeDropzone');
  const knowledgeFileName = document.getElementById('knowledgeFileName');
  const statusEl = document.getElementById('knowledgeUploadStatus');
  const refreshBtn = document.getElementById('refreshKnowledgeBtn');
  const retrieveQueryInput = document.getElementById('retrieveQueryInput');
  const runRetrieveBtn = document.getElementById('runRetrieveBtn');
  const retrieveResults = document.getElementById('retrieveResults');
  loadKnowledge();
  bindUploadDropzone(knowledgeDropzone, fileInput, knowledgeFileName);
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
    statusEl.textContent = '正在上传到默认百炼知识库...';
    try {
      const resp = await fetch('/api/bailian/files/upload', { method: 'POST', body: formData });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '上传失败');
      statusEl.textContent = '上传完成，正在自动重建索引...';
      fileInput.value = '';
      if (knowledgeFileName) knowledgeFileName.textContent = '未选择文件';
      uploadDialog.close();
      await rebuildKnowledgeIndex('上传文档后');
      await loadKnowledge();
    } catch (err) {
      statusEl.textContent = '上传失败：' + err.message;
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
