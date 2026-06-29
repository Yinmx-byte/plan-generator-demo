const pageHost = document.getElementById('pageHost');
const pageTitle = document.getElementById('pageTitle');
const pageEyebrow = document.getElementById('pageEyebrow');
const navItems = [...document.querySelectorAll('.nav-item')];
const resetBtn = document.getElementById('resetBtn');
const refreshAllBtn = document.getElementById('refreshAllBtn');
const planSessionStorageKey = 'planGeneratorTabState';

const pageMeta = {
  plan: { title: '方案生成', eyebrow: 'Conversation' },
  skills: { title: 'Skill 管理', eyebrow: 'AgentScope' },
  knowledge: { title: '知识库', eyebrow: 'RAG' },
  'page-agent': { title: 'Page Agent', eyebrow: 'MCP' },
};

function readPlanSessionState() {
  try {
    return JSON.parse(sessionStorage.getItem(planSessionStorageKey) || '{}');
  } catch (_err) {
    return {};
  }
}

let planSessionState = readPlanSessionState();
let sessionId = planSessionState.sessionId || null;
let currentPage = 'plan';
let messagesEl = null;
let pageAgentMessagesEl = null;
let showToolTrace = Boolean(planSessionState.showToolTrace);
let currentDocumentFileId = planSessionState.fileId || null;
let currentDocumentDownloadUrl = planSessionState.downloadUrl || '';
let currentDocumentFilename = planSessionState.filename || '';
let currentDocumentData = null;
let documentJsonEditor = null;
let documentPreview = null;
let documentOutline = null;
let documentStatus = null;
let documentTitleEl = null;
let documentDownloadLink = null;
let documentEditStatus = null;
let documentDialog = null;
let documentVisualDirty = false;

localStorage.removeItem('planGeneratorSessionId');

function persistPlanState() {
  planSessionState = {
    sessionId,
    messagesHtml: messagesEl?.innerHTML || planSessionState.messagesHtml || '',
    showToolTrace,
    fileId: currentDocumentFileId,
    downloadUrl: currentDocumentDownloadUrl,
    filename: currentDocumentFilename,
  };
  sessionStorage.setItem(planSessionStorageKey, JSON.stringify(planSessionState));
}

function bindStoredDocumentActions(container = messagesEl) {
  container?.querySelectorAll('.document-open-btn').forEach((button) => {
    button.addEventListener('click', () => {
      openGeneratedDocument(
        button.dataset.fileId || currentDocumentFileId,
        button.dataset.downloadUrl || currentDocumentDownloadUrl,
        button.dataset.filename || currentDocumentFilename,
      );
    });
  });
}

function scrollToBottom(el) {
  if (el) el.scrollTop = el.scrollHeight;
}

function applyTraceVisibility(container = messagesEl) {
  container?.classList.toggle('hide-trace', !showToolTrace);
}

function shouldRenderMarkdown(role) {
  return role === 'assistant';
}

function renderInlineMarkdown(value) {
  let html = escapeHtml(value);
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  return html;
}

function splitMarkdownTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function isMarkdownTableSeparator(line) {
  return /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$/.test(line.trim());
}

function renderMarkdownTable(lines) {
  const headers = splitMarkdownTableRow(lines[0]);
  const rows = lines.slice(2).map(splitMarkdownTableRow);
  const headHtml = headers.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join('');
  const bodyHtml = rows
    .map((row) => `<tr>${row.map((cell) => `<td>${renderInlineMarkdown(cell)}</td>`).join('')}</tr>`)
    .join('');
  return `<div class="md-table-wrap"><table><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`;
}

function renderMarkdown(text) {
  const source = String(text || '').replace(/\r\n/g, '\n');
  if (!source.trim()) return '';

  const codeBlocks = [];
  const protectedSource = source.replace(/```[^\n]*\n?([\s\S]*?)```/g, (_match, code) => {
    const index = codeBlocks.push(`<pre class="md-code"><code>${escapeHtml(code.trimEnd())}</code></pre>`) - 1;
    return `\u0000CODE${index}\u0000`;
  });

  const lines = protectedSource.split('\n');
  const html = [];
  let paragraph = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${paragraph.map(renderInlineMarkdown).join('<br>')}</p>`);
    paragraph = [];
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();

    const codeMatch = trimmed.match(/^\u0000CODE(\d+)\u0000$/);
    if (codeMatch) {
      flushParagraph();
      html.push(codeBlocks[Number(codeMatch[1])] || '');
      continue;
    }

    if (!trimmed) {
      flushParagraph();
      continue;
    }

    if (/^-{3,}$/.test(trimmed)) {
      flushParagraph();
      html.push('<hr>');
      continue;
    }

    const heading = trimmed.match(/^(#{2,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      const level = Math.min(heading[1].length, 4);
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    if (splitMarkdownTableRow(line).length > 1 && lines[i + 1] && isMarkdownTableSeparator(lines[i + 1])) {
      flushParagraph();
      const tableLines = [line, lines[i + 1]];
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        tableLines.push(lines[i]);
        i += 1;
      }
      i -= 1;
      html.push(renderMarkdownTable(tableLines));
      continue;
    }

    const unordered = trimmed.match(/^[-*]\s+(.+)$/);
    const ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const orderedList = Boolean(ordered);
      const tag = orderedList ? 'ol' : 'ul';
      const items = [];
      while (i < lines.length) {
        const candidate = lines[i].trim();
        const match = orderedList ? candidate.match(/^\d+[.)]\s+(.+)$/) : candidate.match(/^[-*]\s+(.+)$/);
        if (!match) break;
        items.push(`<li>${renderInlineMarkdown(match[1])}</li>`);
        i += 1;
      }
      i -= 1;
      html.push(`<${tag}>${items.join('')}</${tag}>`);
      continue;
    }

    paragraph.push(line);
  }

  flushParagraph();
  return html.join('');
}

function getMessageContentEl(el) {
  return el?.querySelector?.(':scope > .msg-content') || el;
}

function setMessageContent(el, text) {
  if (!el) return;
  el.dataset.rawText = text || '';
  const contentEl = getMessageContentEl(el);
  if (el.classList.contains('markdown-body')) {
    contentEl.innerHTML = renderMarkdown(text);
  } else {
    contentEl.textContent = text;
  }
}

function deriveMessageText(div) {
  if (div.dataset.rawText) return div.dataset.rawText;
  const clone = div.cloneNode(true);
  clone.querySelectorAll('.copy-message-btn, .document-message-actions').forEach((node) => node.remove());
  return clone.innerText.trim();
}

async function copyTextToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand('copy');
  textarea.remove();
}

function addCopyButton(div) {
  if (!div || div.querySelector(':scope > .copy-message-btn')) return;
  if (!div.classList.contains('assistant') && !div.classList.contains('user')) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'copy-message-btn';
  const label = div.classList.contains('user') ? '复制问题' : '复制回答';
  button.title = label;
  button.setAttribute('aria-label', button.title);
  button.dataset.label = label;
  button.textContent = label;
  button.addEventListener('click', async (event) => {
    event.stopPropagation();
    const originalText = button.dataset.label || button.textContent;
    try {
      await copyTextToClipboard(deriveMessageText(div));
      button.textContent = '已复制';
      button.classList.add('copied');
      setTimeout(() => {
        button.textContent = originalText;
        button.classList.remove('copied');
      }, 1100);
    } catch (_err) {
      button.textContent = '失败';
      setTimeout(() => {
        button.textContent = originalText;
      }, 1100);
    }
  });
  div.appendChild(button);
}

function ensureMessageContentShell(div, rawText = null) {
  if (!div || div.querySelector(':scope > .msg-content')) return;
  const content = document.createElement('div');
  content.className = 'msg-content';
  const movable = [...div.childNodes].filter((node) => {
    if (node.nodeType !== Node.ELEMENT_NODE) return true;
    return !node.classList.contains('copy-message-btn') && !node.classList.contains('document-message-actions');
  });
  movable.forEach((node) => content.appendChild(node));
  div.insertBefore(content, div.firstChild);
  if (rawText !== null) div.dataset.rawText = rawText;
}

function upgradeStoredMessages(container = messagesEl) {
  container?.querySelectorAll('.msg.user, .msg.assistant').forEach((div) => {
    const rawText = deriveMessageText(div);
    ensureMessageContentShell(div, rawText);
    if (div.classList.contains('assistant')) {
      div.classList.add('markdown-body');
      setMessageContent(div, rawText);
    } else {
      setMessageContent(div, rawText);
    }
    addCopyButton(div);
  });
  if (container === messagesEl) persistPlanState();
}

function addMessage(container, role, text, extraHtml = '') {
  if (!container) return null;
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  if (shouldRenderMarkdown(role)) div.classList.add('markdown-body');
  const content = document.createElement('div');
  content.className = 'msg-content';
  div.appendChild(content);
  setMessageContent(div, text);
  if (extraHtml) {
    const wrap = document.createElement('div');
    wrap.innerHTML = extraHtml;
    div.appendChild(wrap);
  }
  addCopyButton(div);
  container.appendChild(div);
  scrollToBottom(container);
  if (container === messagesEl) persistPlanState();
  return div;
}

function addTraceMessage(container, text) {
  if (!container || !text) return null;
  if (/read_file/i.test(text)) return null;
  const details = document.createElement('details');
  details.className = 'msg trace trace-collapsible';
  const summary = document.createElement('summary');
  const firstLine = text.split('\n')[0] || 'Agent 执行细节';
  summary.textContent = firstLine.replace(/^调用工具：/, '调用工具 · ').replace(/^工具返回：/, '工具返回 · ');
  const pre = document.createElement('pre');
  pre.textContent = text;
  details.appendChild(summary);
  details.appendChild(pre);
  container.appendChild(details);
  applyTraceVisibility(container);
  scrollToBottom(container);
  if (container === messagesEl) persistPlanState();
  return details;
}

async function typeInto(el, text) {
  let current = '';
  setMessageContent(el, '');
  const step = Math.max(1, Math.ceil(text.length / 90));
  for (let i = 0; i < text.length; i += step) {
    current += text.slice(i, i + step);
    setMessageContent(el, current);
    scrollToBottom(el.parentElement);
    await new Promise((resolve) => setTimeout(resolve, 8));
  }
  persistPlanState();
}

function setActiveNav(page) {
  navItems.forEach((item) => item.classList.toggle('active', item.dataset.page === page));
}

async function loadPage(page) {
  if (currentPage === 'plan') persistPlanState();
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

function setDocumentEditorState(status, message) {
  if (documentStatus) documentStatus.textContent = status;
  if (documentEditStatus) documentEditStatus.textContent = message || '';
}

function setDocumentDownload(url, filename) {
  currentDocumentDownloadUrl = url || '';
  currentDocumentFilename = filename || '';
  if (!documentDownloadLink) return;
  if (!url) {
    documentDownloadLink.href = '#';
    documentDownloadLink.classList.add('disabled');
    documentDownloadLink.setAttribute('aria-disabled', 'true');
    documentDownloadLink.textContent = '下载 DOCX';
    persistPlanState();
    return;
  }
  documentDownloadLink.href = url;
  documentDownloadLink.classList.remove('disabled');
  documentDownloadLink.removeAttribute('aria-disabled');
  documentDownloadLink.textContent = filename ? `下载 ${filename}` : '下载 DOCX';
  persistPlanState();
}

function collectDocumentSections(data) {
  const sections = data?.document?.sections || [];
  if (!Array.isArray(sections)) return [];
  return sections.filter((section) => section && typeof section === 'object');
}

function editableAttrs(extra = '') {
  return `contenteditable="true" spellcheck="false" ${extra}`.trim();
}

function renderDocumentTable(block, sectionIndex, blockIndex) {
  const columns = Array.isArray(block.columns) ? block.columns : [];
  const rows = Array.isArray(block.rows) ? block.rows : [];
  if (!columns.length) return '';
  return `
    <div class="doc-table-wrap">
      <table class="doc-table">
        <thead>
          <tr>
            ${columns.map((column) => `<th>${escapeHtml(column.label || column.key || '')}</th>`).join('')}
          </tr>
        </thead>
        <tbody>
          ${rows.map((row, rowIndex) => `
            <tr>
              ${columns.map((column) => {
                const key = column.key || column.label || '';
                return `<td ${editableAttrs(`data-table-cell data-section-index="${sectionIndex}" data-block-index="${blockIndex}" data-row-index="${rowIndex}" data-column-key="${escapeHtml(key)}"`)}>${escapeHtml(row?.[key] ?? '')}</td>`;
              }).join('')}
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderDocumentBlock(block, sectionIndex, blockIndex) {
  if (typeof block === 'string') {
    return `<p ${editableAttrs(`data-block-text data-section-index="${sectionIndex}" data-block-index="${blockIndex}"`)}>${escapeHtml(block)}</p>`;
  }
  if (!block || typeof block !== 'object') return '';
  const type = block.type || 'paragraph';
  if (type === 'heading') {
    const level = Math.min(Math.max(Number(block.level || 3), 2), 4);
    return `<h${level} ${editableAttrs(`data-block-text data-section-index="${sectionIndex}" data-block-index="${blockIndex}"`)}>${escapeHtml(block.text || '')}</h${level}>`;
  }
  if (type === 'paragraph') {
    const className = block.bold ? ' class="strong-line"' : '';
    return `<p${className} ${editableAttrs(`data-block-text data-section-index="${sectionIndex}" data-block-index="${blockIndex}"`)}>${escapeHtml(block.text || '')}</p>`;
  }
  if (type === 'paragraphs') {
    const items = Array.isArray(block.items) ? block.items : [];
    return items.map((item, itemIndex) => `<p ${editableAttrs(`data-block-item data-section-index="${sectionIndex}" data-block-index="${blockIndex}" data-item-index="${itemIndex}"`)}>${escapeHtml(item)}</p>`).join('');
  }
  if (type === 'numbered_list') {
    const items = Array.isArray(block.items) ? block.items : [];
    return `<ol>${items.map((item, itemIndex) => `<li ${editableAttrs(`data-block-item data-section-index="${sectionIndex}" data-block-index="${blockIndex}" data-item-index="${itemIndex}"`)}>${escapeHtml(item)}</li>`).join('')}</ol>`;
  }
  if (type === 'plain_list') {
    const items = Array.isArray(block.items) ? block.items : [];
    return `<ul>${items.map((item, itemIndex) => `<li ${editableAttrs(`data-block-item data-section-index="${sectionIndex}" data-block-index="${blockIndex}" data-item-index="${itemIndex}"`)}>${escapeHtml(item)}</li>`).join('')}</ul>`;
  }
  if (type === 'checkbox_group') {
    const items = Array.isArray(block.items) ? block.items : [];
    return `
      <div class="doc-checkbox-grid">
        ${items.map((item, itemIndex) => {
          const checked = item?.checked ? 'checked' : '';
          return `<span class="${checked}" data-checkbox-item data-section-index="${sectionIndex}" data-block-index="${blockIndex}" data-item-index="${itemIndex}"><i></i><b ${editableAttrs('')}>${escapeHtml(item?.label || '')}${item?.extra ? ` ${escapeHtml(item.extra)}` : ''}</b></span>`;
        }).join('')}
      </div>
    `;
  }
  if (type === 'key_values') {
    const items = Array.isArray(block.items) ? block.items : [];
    return `
      <dl class="doc-key-values">
        ${items.map((item, itemIndex) => `
          <div>
            <dt>${escapeHtml(item?.label || '')}</dt>
            <dd ${editableAttrs(`data-key-value data-section-index="${sectionIndex}" data-block-index="${blockIndex}" data-item-index="${itemIndex}"`)}>${escapeHtml(item?.value || '')}</dd>
          </div>
        `).join('')}
      </dl>
    `;
  }
  if (type === 'table') {
    return renderDocumentTable(block, sectionIndex, blockIndex);
  }
  if (type === 'spacer') {
    return '<div class="doc-spacer"></div>';
  }
  return `<p>${escapeHtml(block.text || block.content || '')}</p>`;
}

function renderDocumentPreview(data) {
  if (!documentPreview) return;
  const title = data?.title || data?.document?.title || '未命名检修方案';
  const department = data?.department || data?.document?.department || '云运营中心平台运维处';
  const dateText = data?.date || data?.document?.date || '';
  const sections = collectDocumentSections(data);
  if (documentTitleEl) documentTitleEl.textContent = title;
  if (!sections.length) {
    if (documentOutline) {
      documentOutline.innerHTML = `
        <div class="empty-document">
          <strong>文档结构为空</strong>
          <span>请检查 JSON 编辑区是否包含 document.sections。</span>
        </div>
      `;
    }
    documentPreview.innerHTML = `
      <div class="empty-document">
        <strong>文档结构为空</strong>
        <span>请检查 JSON 编辑区是否包含 document.sections。</span>
      </div>
    `;
    return;
  }
  if (documentOutline) {
    documentOutline.innerHTML = `
      <div class="outline-title">文档章节</div>
      <div class="outline-list">
        ${sections.map((section, index) => `
          <a href="#doc-section-${index + 1}">
            <span>${index + 1}</span>
            <strong>${escapeHtml(section.heading || section.title || '未命名章节')}</strong>
          </a>
        `).join('')}
      </div>
    `;
  }
  documentPreview.innerHTML = `
    <article class="doc-page">
      <header class="doc-cover">
        <div class="doc-logo">国网</div>
        <h1 ${editableAttrs('data-doc-field="title"')}>${escapeHtml(title)}</h1>
        <p ${editableAttrs('data-doc-field="department"')}>${escapeHtml(department)}</p>
        <time ${editableAttrs('data-doc-field="date"')}>${escapeHtml(dateText || '')}</time>
      </header>
      ${sections.map((section, index) => {
        const blocks = Array.isArray(section.blocks) ? section.blocks : [];
        return `
          <section class="doc-section" id="doc-section-${index + 1}">
            <h2 ${editableAttrs(`data-section-heading data-section-index="${index}"`)}>${escapeHtml(section.heading || section.title || '未命名章节')}</h2>
            ${blocks.map((block, blockIndex) => renderDocumentBlock(block, index, blockIndex)).join('')}
          </section>
        `;
      }).join('')}
    </article>
  `;
  documentPreview.querySelectorAll('.outline-list a').forEach((link) => {
    link.addEventListener('click', (event) => event.preventDefault());
  });
  if (documentOutline) {
    documentOutline.querySelectorAll('.outline-list a').forEach((link) => {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        const target = documentPreview.querySelector(link.getAttribute('href'));
        target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
  }
  bindDocumentVisualEditing();
}

function getDocumentBlock(data, element) {
  const sectionIndex = Number(element.dataset.sectionIndex);
  const blockIndex = Number(element.dataset.blockIndex);
  const section = data?.document?.sections?.[sectionIndex];
  return section?.blocks?.[blockIndex] || null;
}

function textFromEditable(element) {
  return (element.innerText || element.textContent || '').replace(/\u00a0/g, ' ').trim();
}

function bindDocumentVisualEditing() {
  if (!documentPreview) return;
  documentVisualDirty = false;
  documentPreview.querySelectorAll('[contenteditable="true"]').forEach((element) => {
    element.addEventListener('input', () => {
      documentVisualDirty = true;
      setDocumentEditorState('编辑中', '已修改文档预览内容，点击“保存并重新渲染”生成新的 DOCX。');
    });
  });
  documentPreview.querySelectorAll('[data-checkbox-item]').forEach((element) => {
    element.addEventListener('click', (event) => {
      if (event.target?.matches?.('[contenteditable="true"]')) return;
      element.closest('.doc-checkbox-grid')
        ?.querySelectorAll('[data-checkbox-item]')
        .forEach((item) => item.classList.remove('checked'));
      element.classList.add('checked');
      documentVisualDirty = true;
      setDocumentEditorState('编辑中', '已修改文档预览内容，点击“保存并重新渲染”生成新的 DOCX。');
    });
  });
}

function syncDocumentPreviewToData(data) {
  if (!documentPreview || !data || !data.document) return data;
  const synced = structuredClone(data);
  documentPreview.querySelectorAll('[data-doc-field]').forEach((element) => {
    const field = element.dataset.docField;
    const value = textFromEditable(element);
    if (field === 'title') {
      synced.title = value;
      synced.document.title = value;
    } else if (field === 'department') {
      synced.department = value;
      synced.document.department = value;
    } else if (field === 'date') {
      synced.date = value;
      synced.document.date = value;
    }
  });
  documentPreview.querySelectorAll('[data-section-heading]').forEach((element) => {
    const section = synced.document.sections?.[Number(element.dataset.sectionIndex)];
    if (section) section.heading = textFromEditable(element);
  });
  documentPreview.querySelectorAll('[data-block-text]').forEach((element) => {
    const block = getDocumentBlock(synced, element);
    if (block) block.text = textFromEditable(element);
  });
  documentPreview.querySelectorAll('[data-block-item]').forEach((element) => {
    const block = getDocumentBlock(synced, element);
    const itemIndex = Number(element.dataset.itemIndex);
    if (block && Array.isArray(block.items)) block.items[itemIndex] = textFromEditable(element);
  });
  documentPreview.querySelectorAll('[data-key-value]').forEach((element) => {
    const block = getDocumentBlock(synced, element);
    const itemIndex = Number(element.dataset.itemIndex);
    if (block?.items?.[itemIndex]) block.items[itemIndex].value = textFromEditable(element);
  });
  documentPreview.querySelectorAll('[data-table-cell]').forEach((element) => {
    const block = getDocumentBlock(synced, element);
    const rowIndex = Number(element.dataset.rowIndex);
    const columnKey = element.dataset.columnKey;
    if (block?.rows?.[rowIndex] && columnKey) block.rows[rowIndex][columnKey] = textFromEditable(element);
  });
  documentPreview.querySelectorAll('[data-checkbox-item]').forEach((element) => {
    const block = getDocumentBlock(synced, element);
    const itemIndex = Number(element.dataset.itemIndex);
    const item = block?.items?.[itemIndex];
    if (item) {
      item.checked = element.classList.contains('checked');
      const labelEl = element.querySelector('b');
      if (labelEl) item.label = textFromEditable(labelEl);
    }
  });
  return synced;
}

async function loadGeneratedDocument(fileId, downloadUrl = '', filename = '') {
  if (!fileId) return;
  currentDocumentFileId = fileId;
  setDocumentEditorState('加载中', '正在读取生成文档结构...');
  setDocumentDownload(downloadUrl, filename);
  try {
    const resp = await fetch(`/api/documents/${encodeURIComponent(fileId)}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '读取文档失败');
    currentDocumentData = data.data;
    if (documentJsonEditor) {
      documentJsonEditor.value = JSON.stringify(currentDocumentData, null, 2);
    }
    renderDocumentPreview(currentDocumentData);
    setDocumentEditorState('可编辑', '已加载生成文档。修改 JSON 后点击“保存并重新渲染”。');
  } catch (err) {
    setDocumentEditorState('加载失败', err.message);
  }
}

async function openGeneratedDocument(fileId, downloadUrl = '', filename = '') {
  if (!documentDialog) return;
  documentDialog.showModal();
  await loadGeneratedDocument(fileId || currentDocumentFileId, downloadUrl, filename);
}

function parseDocumentEditorJson() {
  if (!documentJsonEditor) throw new Error('文档编辑器未初始化');
  try {
    return JSON.parse(documentJsonEditor.value);
  } catch (err) {
    throw new Error(`JSON 格式错误：${err.message}`);
  }
}

async function saveAndRenderDocument() {
  if (!currentDocumentFileId) {
    setDocumentEditorState('未加载', '请先生成一份检修方案。');
    return;
  }
  let edited;
  try {
    edited = documentVisualDirty && currentDocumentData
      ? syncDocumentPreviewToData(currentDocumentData)
      : parseDocumentEditorJson();
    if (documentJsonEditor) documentJsonEditor.value = JSON.stringify(edited, null, 2);
  } catch (err) {
    setDocumentEditorState('JSON错误', err.message);
    return;
  }
  setDocumentEditorState('保存中', '正在保存修改并重新渲染 DOCX...');
  try {
    const saveResp = await fetch(`/api/documents/${encodeURIComponent(currentDocumentFileId)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: edited }),
    });
    const saveData = await saveResp.json();
    if (!saveResp.ok) throw new Error(saveData.detail || '保存失败');

    const renderResp = await fetch(`/api/documents/${encodeURIComponent(currentDocumentFileId)}/render`, {
      method: 'POST',
    });
    const renderData = await renderResp.json();
    if (!renderResp.ok) throw new Error(renderData.detail || '重新渲染失败');

    currentDocumentFileId = renderData.file_id;
    const latestResp = await fetch(`/api/documents/${encodeURIComponent(currentDocumentFileId)}`);
    const latestData = await latestResp.json();
    if (!latestResp.ok) throw new Error(latestData.detail || '读取重渲染文档失败');
    currentDocumentData = latestData.data || saveData.data;
    if (documentJsonEditor) documentJsonEditor.value = JSON.stringify(currentDocumentData, null, 2);
    renderDocumentPreview(currentDocumentData);
    setDocumentDownload(renderData.download_url, renderData.filename);
    documentVisualDirty = false;
    setDocumentEditorState('已保存', '修改已保存，并生成新的 DOCX。');
  } catch (err) {
    setDocumentEditorState('保存失败', err.message);
  }
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
  const showToolTraceInput = document.getElementById('showToolTraceInput');
  documentJsonEditor = document.getElementById('documentJsonEditor');
  documentPreview = document.getElementById('documentPreview');
  documentOutline = document.getElementById('documentOutline');
  documentStatus = document.getElementById('documentStatus');
  documentTitleEl = document.getElementById('documentTitle');
  documentDownloadLink = document.getElementById('documentDownloadLink');
  documentEditStatus = document.getElementById('documentEditStatus');
  documentDialog = document.getElementById('documentDialog');
  const formatDocumentJsonBtn = document.getElementById('formatDocumentJsonBtn');
  const saveDocumentBtn = document.getElementById('saveDocumentBtn');
  if (showToolTraceInput) {
    showToolTraceInput.checked = showToolTrace;
    showToolTraceInput.addEventListener('change', () => {
      showToolTrace = showToolTraceInput.checked;
      applyTraceVisibility(messagesEl);
      persistPlanState();
    });
  }
  applyTraceVisibility(messagesEl);
  if (planSessionState.messagesHtml) {
    messagesEl.innerHTML = planSessionState.messagesHtml;
    upgradeStoredMessages(messagesEl);
    bindStoredDocumentActions();
    applyTraceVisibility(messagesEl);
    scrollToBottom(messagesEl);
  } else {
    addMessage(messagesEl, 'assistant', '请粘贴检修需求描述或需求文档。我会逐步抽取信息、追问缺失项，并在信息齐全后生成 Word 检修方案。');
  }
  setDocumentDownload(currentDocumentDownloadUrl, currentDocumentFilename);
  setDocumentEditorState(currentDocumentFileId ? '可编辑' : '未加载', currentDocumentFileId ? '已保留上一份生成文档。' : '未生成文档。');
  if (currentDocumentData) {
    if (documentJsonEditor) documentJsonEditor.value = JSON.stringify(currentDocumentData, null, 2);
    renderDocumentPreview(currentDocumentData);
  }

  formatDocumentJsonBtn?.addEventListener('click', () => {
    try {
      const data = parseDocumentEditorJson();
      currentDocumentData = data;
      documentVisualDirty = false;
      documentJsonEditor.value = JSON.stringify(data, null, 2);
      renderDocumentPreview(data);
      setDocumentEditorState('已格式化', 'JSON 已格式化，尚未保存。');
    } catch (err) {
      setDocumentEditorState('JSON错误', err.message);
    }
  });
  saveDocumentBtn?.addEventListener('click', saveAndRenderDocument);

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
      persistPlanState();
    }
    if (event === 'status' || event === 'collected') {
      addMessage(messagesEl, 'status', data.message || '处理中...');
      return;
    }
    if (event === 'trace') {
      if (showToolTrace) addTraceMessage(messagesEl, data.message || 'Agent 执行中...');
      return;
    }
    if (event === 'evidence') {
      const skills = (data.selected_skills || []).join('、') || '未命中明确 Skill';
      const ragText = data.rag_enabled
        ? `RAG 已启用，状态 ${data.rag_status || 'unknown'}，命中 ${data.rag_chunks_count || 0} 个参考片段`
        : 'RAG 未启用，当前仅使用 Skill 规则与用户输入';
      addMessage(messagesEl, 'status', `${data.message || '生成依据已准备完成'}\n候选 Skill：${skills}\n${ragText}`);
      return;
    }
    if (event === 'done') {
      if (data.status === 'generated') {
        const validationText = data.validation_result ? `\n\nPage Agent 验证结果：\n${data.validation_result}` : '';
        const msg = addMessage(messagesEl, 'assistant', '');
        await typeInto(msg, data.message + validationText);
        const generated = data.generated || {};
        currentDocumentFileId = generated.file_id || data.file_id;
        currentDocumentDownloadUrl = data.download_url || generated.download_url || '';
        currentDocumentFilename = data.filename || generated.filename || '';
        const wrap = document.createElement('div');
        wrap.className = 'document-message-actions';
        wrap.innerHTML = `
          <button class="document-open-btn" type="button"
            data-file-id="${escapeHtml(currentDocumentFileId || '')}"
            data-download-url="${escapeHtml(currentDocumentDownloadUrl)}"
            data-filename="${escapeHtml(currentDocumentFilename)}">查看/编辑文档</button>
          <a class="download" href="${escapeHtml(currentDocumentDownloadUrl)}">下载检修方案</a>
        `;
        msg.appendChild(wrap);
        bindStoredDocumentActions(wrap);
        setDocumentDownload(currentDocumentDownloadUrl, currentDocumentFilename);
        persistPlanState();
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
  currentDocumentFileId = null;
  currentDocumentDownloadUrl = '';
  currentDocumentFilename = '';
  currentDocumentData = null;
  planSessionState = {};
  sessionStorage.removeItem(planSessionStorageKey);
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
