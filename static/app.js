const state = { video: null, online: false, stale: false, restarting: false, directorySelecting: false, meeting: { connected: false, taskId: null }, taskOptions: JSON.parse(localStorage.getItem('bbdown-task-options') || '{}'), transcription: { provider: 'local', model: '', label: '', mimo_api_key_configured: false, mimo_api_key_hint: '' }, up: { input: '', profile: null, items: [], selected: {}, page: 1, total: 0, totalPages: 0, period: 'all', keyword: '', loading: false, tab: 'posts', collections: [], collectionPage: 1, collectionTotal: 0, collectionTotalPages: 0, collectionLoading: false, postsError: '', collectionsError: '', detail: null }, asr: { health: null, file: null, duration: null, job: null, result: null, polling: null, tasks: [], activeTaskId: null, manageMode: false, selectedTaskIds: [] } };
const $ = (selector) => document.querySelector(selector);
const PLACEHOLDER_COVER = `data:image/svg+xml;charset=utf-8,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="328" height="184" viewBox="0 0 328 184"><rect width="328" height="184" fill="#eef0f4"/><path d="M132 78h64v28h-64z" fill="#c5ccd7"/><circle cx="148" cy="88" r="5" fill="#eef0f4"/><path d="m139 101 15-14 10 9 11-12 14 17z" fill="#eef0f4"/><text x="164" y="132" text-anchor="middle" fill="#8d98a8" font-size="14">封面暂时无法加载</text></svg>')}`;
let loginTimer = null;

async function api(path, options = {}) {
  let response;
  const { timeoutMs = 5000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timeout = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try { response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, signal: controller.signal, ...fetchOptions }); }
  catch (_) { if (!state.directorySelecting) setOffline(); throw new Error('本地服务已断开，请重新启动工具。'); }
  finally { if (timeout) clearTimeout(timeout); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(data.error || '请求失败，请稍后重试。'); error.status = response.status; error.data = data; throw error; }
  return data;
}
async function uploadApi(path, formData, timeoutMs = 0) {
  let response;
  const controller = new AbortController();
  const timeout = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try { response = await fetch(path, { method: 'POST', body: formData, signal: controller.signal }); }
  catch (_) { throw new Error('本地服务已断开，请重新启动工具。'); }
  finally { if (timeout) clearTimeout(timeout); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(data.error || '请求失败，请稍后重试。'); error.status = response.status; error.data = data; throw error; }
  return data;
}

function formatDuration(seconds) { const m = Math.floor(seconds / 60); const s = seconds % 60; return `${m}:${String(s).padStart(2, '0')}`; }
function formatSpeed(speed) { if (!speed) return '速度：等待 BBDownNext 更新'; const units = ['B/s', 'KB/s', 'MB/s', 'GB/s']; let index = 0; while (speed >= 1024 && index < units.length - 1) { speed /= 1024; index++; } return `速度：${speed.toFixed(index ? 1 : 0)} ${units[index]}`; }
function escapeHtml(value) { const element = document.createElement('span'); element.textContent = value || ''; return element.innerHTML; }
function titleForStatus(task) { if (task.status === 'Finished') { if (task.isCancelled) return '已取消'; return task.isSuccessful ? '已完成' : '下载失败'; } return ({ Pending: '等待开始', Queued: '排队中', Running: '下载中' }[task.status] || task.status || '等待中'); }

function setServiceState(health) {
  const badge = $('#service-state');
  const problem = health.dependency_problem;
  const runningBuild = health.running_build_id || health.build_id || '';
  const diskBuild = health.disk_build_id || runningBuild;
  if (!health.running_build_id || health.stale || (runningBuild && diskBuild && runningBuild !== diskBuild)) {
    state.online = false; state.stale = true;
    badge.textContent = '正在更新本地服务…'; badge.className = 'status-pill problem';
    $('#task-list').innerHTML = '<div class="task-empty">后台正在更新，请稍候…</div>';
    state.up.profile = null; state.up.items = []; state.up.collections = []; state.up.detail = null;
    $('#up-message').textContent = '后台版本已更新，正在重新连接…'; renderUp();
    if (!state.restarting) restartLocalService();
    return;
  }
  state.stale = false;
  state.online = true;
  badge.textContent = problem || (health.download_dir ? '本地服务已连接' : '等待设置目录');
  badge.className = `status-pill ${problem ? 'problem' : health.download_dir ? 'ready' : ''}`;
  $('#first-run').classList.toggle('hidden', Boolean(health.download_dir));
  $('#download-dir').textContent = health.download_dir || '尚未设置';
}

function setOffline() {
  if (state.directorySelecting) return;
  state.online = false;
  const badge = $('#service-state');
  badge.textContent = '本地服务已断开，请重新启动工具';
  badge.className = 'status-pill problem';
}

async function refreshHealth() { try { setServiceState(await api('/api/health')); } catch (_) { setOffline(); } }

async function refreshMeetingStatus() {
  try {
    const bridge = await api('/api/meeting-bridge/status');
    state.meeting.connected = Boolean(bridge.connected);
    $('#meeting-bridge-settings-status').textContent = bridge.connected
      ? `浏览器插件已连接${bridge.browser ? `（${bridge.browser === 'edge' ? 'Edge' : 'Chrome'}）` : ''}`
      : '未检测到 CourseFlow 浏览器插件';
    $('#meeting-parse').disabled = !bridge.connected;
    if (!state.meeting.taskId) {
      $('#meeting-message').textContent = bridge.connected ? '' : '未检测到 CourseFlow 浏览器插件。请在 Chrome 或 Edge 中启用桥接扩展。';
      return;
    }
    const task = await api(`/api/meeting-bridge/tasks/${state.meeting.taskId}`);
    const messages = {
      pending: '等待浏览器插件领取解析任务…',
      processing: ({
        opening: '正在打开回放…',
        waiting_page: '正在等待回放页面…',
        waiting_media: '正在识别媒体…',
        submitting: '正在回传解析结果…'
      })[task.stage] || '浏览器插件正在解析回放…',
      success: '解析成功，已找到视频资源。下载功能将在下一阶段接入。',
      failed: task.error?.message || '回放解析失败。'
    };
    $('#meeting-message').textContent = messages[task.status] || '等待浏览器插件…';
    if (task.status === 'success') {
      renderMeetingResult({ title: task.title, duration_seconds: task.duration_seconds, media_found: task.media_found });
      $('#meeting-parse').disabled = !bridge.connected;
    } else {
      $('#meeting-result').classList.add('hidden');
      $('#meeting-parse').disabled = !bridge.connected || ['pending', 'processing'].includes(task.status);
    }
  } catch (_) { /* 本地服务连接状态由主 health 检查负责显示 */ }
}

function renderMeetingResult(result) {
  $('#meeting-result-title').textContent = result.title || '腾讯会议回放';
  const duration = result.duration_seconds ? `时长 ${formatMeetingDuration(result.duration_seconds)}` : '页面未提供时长';
  const media = result.media_found ? '已找到视频资源。' : '未能确认视频资源。';
  $('#meeting-result-meta').textContent = `${duration} · ${media}`;
  $('#meeting-result').classList.remove('hidden');
  $('#meeting-message').textContent = '回放解析已完成，下载功能将在下一阶段接入。';
}

async function parseMeetingRecording() {
  const url = $('#meeting-url').value.trim();
  if (!url) { $('#meeting-message').textContent = '请先粘贴腾讯会议回放链接。'; return; }
  if (!state.meeting.connected) { $('#meeting-message').textContent = '未检测到 CourseFlow 浏览器插件，请先启用扩展后重试。'; return; }
  $('#meeting-result').classList.add('hidden');
  $('#meeting-parse').disabled = true;
  $('#meeting-message').textContent = '正在创建解析任务…';
  try {
    const response = await api('/api/meeting-bridge/tasks', { method: 'POST', body: JSON.stringify({ url }) });
    state.meeting.taskId = response.item.task_id;
    await refreshMeetingStatus();
  }
  catch (error) { $('#meeting-message').textContent = error.message; $('#meeting-parse').disabled = false; }
}

function formatMeetingDuration(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const parts = [seconds % 60, Math.floor(seconds / 60) % 60];
  if (Math.floor(seconds / 3600)) parts.push(Math.floor(seconds / 3600));
  return parts.reverse().map((part, index) => index ? String(part).padStart(2, '0') : String(part)).join(':');
}

async function restartLocalService() {
  if (state.restarting) return;
  state.restarting = true;
  try { await api('/api/restart', { method: 'POST', body: '{}', timeoutMs: 3000 }); } catch (_) { /* 旧进程可能在响应前退出 */ }
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    try {
      const health = await api('/api/health', { timeoutMs: 1500 });
      const runningBuild = health.running_build_id || health.build_id || '';
      const diskBuild = health.disk_build_id || runningBuild;
      if (health.online && !health.stale && runningBuild && runningBuild === diskBuild) { window.location.reload(); return; }
    } catch (_) { /* 等待新实例 */ }
  }
  state.restarting = false; setOffline();
}

function renderVideo(video) {
  state.video = video;
  const cover = $('#video-cover');
  cover.onerror = () => { cover.onerror = null; cover.src = PLACEHOLDER_COVER; };
  cover.src = `/api/cover?url=${encodeURIComponent(video.cover)}`;
  $('#video-title').textContent = video.title;
  $('#video-owner').textContent = `UP 主：${video.owner}`;
  $('#page-list').innerHTML = video.pages.map((page) => `<label class="page-item"><input type="checkbox" value="${page.page}" checked><span class="page-number">P${page.page}</span><span class="page-name">${escapeHtml(page.title)}</span><span class="page-duration">${formatDuration(page.duration)}</span></label>`).join('');
  $('#video-result').classList.remove('hidden');
}

async function applyUpFilter() {
  if (!state.up.input) { $('#up-message').textContent = '请先获取 UP 主投稿。'; return; }
  state.up.period = $('#up-period').value; state.up.keyword = $('#up-keyword').value.trim(); await loadUpPage(true);
}

async function parseUp() {
  const input = $('#up-url').value.trim(); if (!input) { $('#up-message').textContent = '请输入 UP 主主页链接或 mid。'; return; }
  if (state.up.input && state.up.input !== input) state.up.selected = {};
  state.up.input = input; state.up.period = $('#up-period').value; state.up.keyword = $('#up-keyword').value.trim(); state.up.collections = []; state.up.collectionTotal = 0; state.up.detail = null;
  $('#up-parse-button').disabled = true;
  try { await loadUpPage(true); if (state.up.profile) await loadCollections(true); } finally { $('#up-parse-button').disabled = false; }
}

function renderUpItemList(containerId, emptyId, items) {
  const container = $(`#${containerId}`);
  container.innerHTML = items.map((item) => `<label class="up-item"><input type="checkbox" data-up-id="${escapeHtml(item.bvid)}" ${state.up.selected[item.bvid] ? 'checked' : ''}><img class="up-cover" alt="视频封面" src="/api/cover?url=${encodeURIComponent(item.cover || '')}"><span class="up-item-main"><strong>${escapeHtml(item.title)}</strong><span class="up-item-meta">${escapeHtml(item.publish_time)} · ${escapeHtml(item.duration_text)}</span></span></label>`).join('');
  container.querySelectorAll('.up-cover').forEach((image) => { image.onerror = () => { image.onerror = null; image.src = PLACEHOLDER_COVER; }; });
  $(`#${emptyId}`).classList.toggle('hidden', items.length > 0);
}

function renderPagination(containerId, page, totalPages, onPage) {
  const container = $(`#${containerId}`);
  if (!totalPages) { container.innerHTML = ''; return; }
  const pages = [];
  const add = (value) => { if (!pages.includes(value)) pages.push(value); };
  add(1); add(totalPages); for (let value = Math.max(1, page - 2); value <= Math.min(totalPages, page + 2); value += 1) add(value);
  pages.sort((a, b) => a - b);
  const parts = []; let previous = 0;
  pages.forEach((value) => { if (previous && value - previous > 1) parts.push('<span class="pagination-gap">…</span>'); parts.push(`<button class="secondary page-button ${value === page ? 'active' : ''}" data-page="${value}">${value}</button>`); previous = value; });
  container.innerHTML = `<button class="secondary page-button" data-page="${Math.max(1, page - 1)}" ${page <= 1 ? 'disabled' : ''}>上一页</button>${parts.join('')}<button class="secondary page-button" data-page="${Math.min(totalPages, page + 1)}" ${page >= totalPages ? 'disabled' : ''}>下一页</button>`;
  container.querySelectorAll('.page-button:not(:disabled)').forEach((button) => button.addEventListener('click', () => onPage(Number(button.dataset.page))));
}

function renderUp() {
  const up = state.up;
  if (!up.profile) { $('#up-result').classList.add('hidden'); return; }
  $('#up-result').classList.remove('hidden');
  const avatar = $('#up-avatar'); avatar.onerror = () => { avatar.onerror = null; avatar.src = PLACEHOLDER_COVER; }; avatar.src = up.profile.face ? `/api/cover?url=${encodeURIComponent(up.profile.face)}` : PLACEHOLDER_COVER;
  $('#up-name').textContent = up.profile.name; $('#up-total').textContent = `投稿 ${up.profile.total} 个 · 当前筛选 ${up.total} 个`;
  $('#up-post-count').textContent = up.total; $('#up-collection-count').textContent = up.collectionsError ? '加载失败' : (up.collectionTotal || '—'); $('#up-selected-count').textContent = `已选择 ${Object.keys(up.selected).length} 个`;
  $('#up-tab-posts').classList.toggle('active', up.tab === 'posts'); $('#up-tab-collections').classList.toggle('active', up.tab === 'collections');
  $('#up-page-summary').textContent = up.postsError || `共 ${up.total} 个投稿 · 第 ${up.totalPages ? up.page : 0} / ${up.totalPages || 0} 页`;
  renderUpItemList('up-list', 'up-empty', up.items); renderPagination('up-pagination', up.page, up.totalPages, async (page) => { up.page = page; await loadUpPage(); });
  $('#up-download-button').disabled = !Object.keys(up.selected).length || up.loading;
  $('#up-posts-panel').classList.toggle('hidden', up.tab !== 'posts'); $('#up-collections-panel').classList.toggle('hidden', up.tab !== 'collections' || Boolean(up.detail)); $('#up-detail-panel').classList.toggle('hidden', up.tab !== 'detail');
  if (up.detail) { $('#up-detail-kind').textContent = up.detail.kind === 'series' ? '系列' : '合集'; $('#up-detail-name').textContent = up.detail.name; $('#up-detail-summary').textContent = `共 ${up.detail.total} 个视频 · 第 ${up.detail.totalPages ? up.detail.page : 0} / ${up.detail.totalPages || 0} 页`; $('#up-detail-selected-count').textContent = `已选择 ${Object.keys(up.selected).length} 个`; renderUpItemList('up-detail-list', 'up-detail-empty', up.detail.items); renderPagination('up-detail-pagination', up.detail.page, up.detail.totalPages, async (page) => { await loadCollectionDetail(page); }); $('#up-detail-download-button').disabled = !Object.keys(up.selected).length || up.detail.loading; }
  renderCollections();
}

function renderCollections() {
  const up = state.up; const container = $('#up-collection-list'); if (!container) return;
  container.innerHTML = up.collections.map((item) => `<button class="collection-item" data-collection-kind="${escapeHtml(item.kind)}" data-collection-id="${escapeHtml(item.id)}"><img class="collection-cover" alt="合集封面" src="/api/cover?url=${encodeURIComponent(item.cover || '')}"><span class="collection-main"><strong>${escapeHtml(item.name)}</strong><span>${item.kind === 'series' ? '系列' : '合集'} · ${item.total} 个视频</span></span></button>`).join('');
  container.querySelectorAll('.collection-cover').forEach((image) => { image.onerror = () => { image.onerror = null; image.src = PLACEHOLDER_COVER; }; });
  container.querySelectorAll('.collection-item').forEach((button) => button.addEventListener('click', () => loadCollectionDetail(1, button.dataset.collectionKind, button.dataset.collectionId)));
  $('#up-collection-empty').classList.toggle('hidden', up.collections.length > 0);
  $('#up-collection-summary').textContent = up.collectionsError || `共 ${up.collectionTotal} 个合集和系列 · 第 ${up.collectionTotalPages ? up.collectionPage : 0} / ${up.collectionTotalPages || 0} 页`;
  renderPagination('up-collection-pagination', up.collectionPage, up.collectionTotalPages, async (page) => { up.collectionPage = page; await loadCollections(); });
}

async function loadUpPage(reset = false) {
  const up = state.up; if (up.loading) return; if (reset) { up.page = 1; up.items = []; up.profile = null; up.total = 0; up.totalPages = 0; up.postsError = ''; up.detail = null; }
  up.loading = true; $('#up-message').textContent = '正在获取投稿…'; renderUp();
  try { const result = await api('/api/up', { method: 'POST', timeoutMs: 60000, body: JSON.stringify({ input: up.input, page: up.page, period: up.period, keyword: up.keyword, page_size: 30 }) }); up.postsError = ''; up.profile = result.profile; up.items = result.items || []; up.total = Number(result.total || 0); up.totalPages = Number(result.total_pages || 0); up.page = Number(result.page || up.page); $('#up-message').textContent = up.items.length ? '' : '没有符合条件的公开视频投稿。'; }
  catch (error) { up.postsError = error.message; $('#up-message').textContent = error.message; up.items = []; up.total = 0; up.totalPages = 0; }
  finally { up.loading = false; renderUp(); if (up.items.length) $('#up-list').scrollIntoView({ block: 'start' }); }
}

async function loadCollections(reset = false) {
  const up = state.up; if (up.collectionLoading) return; if (reset) { up.collectionPage = 1; up.collections = []; up.collectionTotal = 0; up.collectionTotalPages = 0; up.collectionsError = ''; }
  up.collectionLoading = true; $('#up-message').textContent = '正在获取合集和系列…'; renderUp();
  try { const result = await api('/api/up/collections', { method: 'POST', timeoutMs: 30000, body: JSON.stringify({ input: up.input, page: up.collectionPage, page_size: 30 }) }); up.profile = { ...up.profile, ...result.profile, total: up.total || result.profile.total }; up.collections = result.items || []; up.collectionTotal = Number(result.total || 0); up.collectionTotalPages = Number(result.total_pages || 0); up.collectionPage = Number(result.page || up.collectionPage); $('#up-message').textContent = ''; }
  catch (error) { up.collectionsError = error.message; $('#up-message').textContent = error.message; up.collections = []; up.collectionTotal = 0; up.collectionTotalPages = 0; }
  finally { up.collectionLoading = false; renderUp(); }
}

async function loadCollectionDetail(page = 1, kind = state.up.detail?.kind, collectionId = state.up.detail?.id) {
  const up = state.up; if (!kind || !collectionId) return; up.tab = 'detail'; up.detail = { kind, id: collectionId, name: '正在加载…', items: [], total: 0, page, totalPages: 0, loading: true }; renderUp();
  try { const result = await api('/api/up/collection', { method: 'POST', timeoutMs: 30000, body: JSON.stringify({ input: up.input, kind, collection_id: collectionId, page, page_size: 30 }) }); up.profile = { ...up.profile, ...result.profile, total: up.total || result.profile.total }; up.detail = { ...result.collection, items: result.items || [], total: Number(result.total || 0), page: Number(result.page || page), totalPages: Number(result.total_pages || 0), loading: false }; $('#up-message').textContent = ''; }
  catch (error) { up.detail.loading = false; $('#up-message').textContent = error.message; }
  finally { renderUp(); if (up.detail && up.detail.items.length) $('#up-detail-list').scrollIntoView({ block: 'start' }); }
}

function selectUpPage() { state.up.items.forEach((item) => { state.up.selected[item.bvid] = item; }); renderUp(); }
function clearUpPage() { state.up.items.forEach((item) => { delete state.up.selected[item.bvid]; }); renderUp(); }
function clearUpSelected() { state.up.selected = {}; renderUp(); }
function changeUpSelection(event) { const input = event.target; if (!input.dataset.upId) return; const items = state.up.detail ? state.up.detail.items : state.up.items; const item = items.find((candidate) => candidate.bvid === input.dataset.upId); if (!item) return; if (input.checked) state.up.selected[item.bvid] = item; else delete state.up.selected[item.bvid]; renderUp(); }
function openAsrPage() { const button = document.querySelector('.nav-button[data-page="asr"]'); if (button) button.click(); }
async function startTranscriptionBatch(items, buttonId, messageId = 'up-message') {
  const button = $(`#${buttonId}`); if (button) button.disabled = true;
  try {
    const result = await api('/api/transcriptions', { method: 'POST', timeoutMs: 30000, body: JSON.stringify({ items }) });
    state.asr.tasks = [...(result.items || []), ...state.asr.tasks];
    if (messageId && $(`#${messageId}`)) $(`#${messageId}`).textContent = `已加入 ${result.items?.length || 0} 个本地转写任务。`;
    renderAsr(); openAsrPage(); await refreshTranscriptionTasks();
  } catch (error) { if (messageId && $(`#${messageId}`)) $(`#${messageId}`).textContent = error.message; }
  finally { if (button) button.disabled = false; renderUp(); }
}
async function startSelectedBatch(mode, buttonId) {
  const selected = Object.values(state.up.selected);
  if (!selected.length) { $('#up-message').textContent = '请至少选择一个视频。'; return; }
  if (mode === 'transcribe') {
    await startTranscriptionBatch(selected.map((item) => ({ url: item.url, title: item.title, pages: [], all_pages: true })), buttonId);
    return;
  }
  const button = $(`#${buttonId}`); button.disabled = true; let added = 0;
  for (const item of selected) {
    try { const task = await api('/api/tasks', { method: 'POST', body: JSON.stringify({ url: item.url, pages: [], all_pages: true, mode }) }); state.taskOptions[task.id] = { url: item.url, pages: [], all_pages: true, mode }; added += 1; }
    catch (error) { $('#up-message').textContent = `已加入 ${added} 个，部分任务失败：${error.message}`; break; }
  }
  localStorage.setItem('bbdown-task-options', JSON.stringify(state.taskOptions)); if (added === selected.length) $('#up-message').textContent = `已加入 ${added} 个下载任务。`; await refreshTasks(); button.disabled = false; renderUp();
}

async function parseVideo() {
  const value = $('#video-url').value.trim(); const message = $('#parse-message');
  message.textContent = '';
  if (!value) { message.textContent = '请输入视频链接或 BV 号。'; return; }
  const button = $('#parse-button'); button.disabled = true; button.textContent = '正在解析…';
  try { renderVideo(await api('/api/parse', { method: 'POST', body: JSON.stringify({ url: value }) })); }
  catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; button.textContent = '解析视频'; }
}

function selectedPages() { return [...document.querySelectorAll('#page-list input:checked')].map((input) => input.value); }
async function createTask() {
  if (!state.video) return;
  const pages = selectedPages();
  if (!pages.length) { $('#parse-message').textContent = '请至少选择一个分 P。'; return; }
  const button = $('#download-button'); button.disabled = true; button.textContent = '正在加入…';
  const mode = document.querySelector('input[name="mode"]:checked').value;
  if (mode === 'transcribe') {
    await startTranscriptionBatch([{ url: state.video.url, title: state.video.title, pages, all_pages: false }], 'download-button', 'parse-message');
    button.textContent = '开始下载';
    return;
  }
  try {
    const task = await api('/api/tasks', { method: 'POST', body: JSON.stringify({ url: state.video.url, pages, mode }) });
    state.taskOptions[task.id] = { url: state.video.url, pages, mode };
    localStorage.setItem('bbdown-task-options', JSON.stringify(state.taskOptions));
    await refreshTasks();
  } catch (error) { $('#parse-message').textContent = error.message; }
  finally { button.disabled = false; button.textContent = '开始下载'; }
}

async function stopTask(id) { try { await api(`/api/tasks/${encodeURIComponent(id)}/stop`, { method: 'POST', body: '{}' }); await refreshTasks(); } catch (error) { alert(error.message); } }
async function retryTask(id) {
  const option = state.taskOptions[id];
  if (!option) { alert('无法恢复该任务的下载选项，请重新添加视频后再试。'); return; }
  try { await api('/api/tasks', { method: 'POST', body: JSON.stringify(option) }); await refreshTasks(); } catch (error) { alert(error.message); }
}
function taskHtml(task) {
  const status = titleForStatus(task); const failed = task.status === 'Finished' && !task.isSuccessful;
  const progress = Math.max(0, Math.min(100, Math.round((task.progress || 0) * 100)));
  const actions = task.status === 'Running' || task.status === 'Queued' ? `<button class="secondary" data-stop="${escapeHtml(task.id)}">取消</button>` : failed && !task.isCancelled ? `<button class="secondary" data-retry="${escapeHtml(task.id)}">重试</button>` : '';
  return `<article class="task"><div class="task-top"><span class="task-name">${escapeHtml(task.title || task.url)}</span><span class="task-status ${failed ? 'failed' : task.isSuccessful ? 'done' : ''}">${status}</span><span class="task-actions">${actions}</span></div><div class="task-progress"><span style="width:${task.isSuccessful ? 100 : progress}%"></span></div><div class="task-meta"><span>进度：${task.isSuccessful ? 100 : progress}%</span><span>${formatSpeed(task.downloadSpeed)}</span></div>${failed && task.errorMessage ? `<div class="task-error">${escapeHtml(task.errorMessage)}</div>` : ''}</article>`;
}
async function refreshTasks() {
  if (!state.online || state.directorySelecting) return;
  try {
    const snapshot = await api('/api/tasks'); const tasks = [...(snapshot.running || []), ...(snapshot.finished || [])];
    $('#task-list').innerHTML = tasks.length ? tasks.map(taskHtml).join('') : '<div class="task-empty">暂无下载任务</div>';
  } catch (error) { $('#task-list').innerHTML = `<div class="task-empty">${escapeHtml(error.message)}</div>`; }
}
async function chooseDirectory() {
  if (state.directorySelecting) return;
  state.directorySelecting = true;
  const buttons = [$('#choose-first-dir'), $('#choose-dir')].filter(Boolean);
  buttons.forEach((button) => { button.disabled = true; button.dataset.originalText = button.textContent; button.textContent = '请选择目录…'; });
  try {
    const result = await api('/api/select-directory', { method: 'POST', body: '{}', timeoutMs: 0 });
    if (!result.cancelled) { await refreshHealth(); await refreshTasks(); if (result.error) alert(result.error); }
  } catch (error) { alert(error.message); }
  finally { state.directorySelecting = false; buttons.forEach((button) => { button.disabled = false; button.textContent = button.dataset.originalText; }); }
}
async function simpleAction(path) { try { await api(path, { method: 'POST', body: '{}' }); } catch (error) { alert(error.message); } }

function renderLoginStatus(result) {
  $('#login-status').textContent = result.name ? `${result.status}：${result.name}` : (result.status || '未登录');
  $('#login-start').classList.toggle('hidden', Boolean(result.logged_in));
  $('#login-logout').classList.toggle('hidden', !result.logged_in);
  $('#login-qr-panel').classList.toggle('hidden', Boolean(result.logged_in) || !$('#login-qr').src);
}
async function refreshLoginStatus() { try { renderLoginStatus(await api('/api/login/status')); } catch (error) { $('#login-status').textContent = error.message; } }
async function startLogin() {
  const button = $('#login-start'); button.disabled = true;
  try {
    const result = await api('/api/login/start', { method: 'POST', body: '{}' });
    $('#login-qr').src = result.qr_image;
    $('#login-qr-link').href = result.qr_url;
    $('#login-qr-panel').classList.remove('hidden'); renderLoginStatus(result);
    if (loginTimer) clearInterval(loginTimer);
    loginTimer = setInterval(async () => { try { const status = await api('/api/login/poll', { method: 'POST', body: '{}' }); renderLoginStatus(status); if (status.logged_in || status.status === '二维码已过期') { clearInterval(loginTimer); loginTimer = null; } } catch (error) { $('#login-status').textContent = error.message; } }, 2000);
  } catch (error) { $('#login-status').textContent = error.message; }
  finally { button.disabled = false; }
}
async function logout() { try { renderLoginStatus(await api('/api/login/logout', { method: 'POST', body: '{}' })); } catch (error) { $('#login-status').textContent = error.message; } }

function renderTranscriptionSettings() {
  const settings = state.transcription;
  $('#transcription-provider').value = settings.provider === 'mimo-v2.5-asr' ? 'mimo-v2.6-flash' : (settings.provider || 'local');
  const cloud = settings.provider !== 'local';
  $('#mimo-key-row').classList.toggle('hidden', !cloud);
  $('#mimo-key-status').textContent = settings.mimo_api_key_configured ? `已配置（${settings.mimo_api_key_hint}）` : '未配置';
}
async function refreshTranscriptionSettings() {
  try { state.transcription = await api('/api/transcription/settings'); renderTranscriptionSettings(); await checkAsrHealth(); }
  catch (error) { $('#transcription-settings-message').textContent = error.message; }
}
async function saveTranscriptionSettings() {
  const button = $('#transcription-save'); const message = $('#transcription-settings-message');
  button.disabled = true; message.textContent = '正在保存并测试…';
  try {
    const body = { provider: $('#transcription-provider').value, api_key: $('#mimo-api-key').value.trim() };
    state.transcription = await api('/api/transcription/settings', { method: 'POST', timeoutMs: 30000, body: JSON.stringify(body) });
    $('#mimo-api-key').value = ''; renderTranscriptionSettings(); message.textContent = '已保存并测试通过。'; await checkAsrHealth();
  } catch (error) { message.textContent = error.message; }
  finally { button.disabled = false; }
}

const ASR_EXTENSIONS = new Set(['m4a', 'mp3', 'wav', 'flac']);
const ASR_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024;
const ASR_STATUS = { preparing: ['正在准备音频', '正在整理本地音频。'], uploading: ['正在上传', '正在发送至你的本机转写服务。'], queued: ['排队中', '等待本机转写服务处理。'], processing: ['正在转写', '正在由本机转写服务处理。'], succeeded: ['转写完成', '文字稿已准备完成。'], failed: ['转写失败', '本次转写没有完成。'], cancelled: ['已取消', '转写任务已取消。'] };
function asrFileName() { return state.asr.file?.name?.replace(/\.[^.]+$/, '') || '文字稿'; }
function formatFileSize(bytes) { if (!Number.isFinite(bytes)) return '大小未知'; if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`; return `${(bytes / 1024 / 1024).toFixed(bytes >= 100 * 1024 * 1024 ? 0 : 1)} MB`; }
function formatAsrDuration(seconds) { const total = Math.max(0, Math.floor(seconds)); const hours = Math.floor(total / 3600); const minutes = Math.floor((total % 3600) / 60); const remainder = total % 60; return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}` : `${minutes}:${String(remainder).padStart(2, '0')}`; }
function asrMessage(value = '') { $('#asr-message').textContent = value; }
const AUTO_ASR_STATUS = { downloading: '获取音频中', uploading: '提交转写中', queued: '排队中', processing: '正在转写', succeeded: '已完成', failed: '失败', cancelled: '已取消' };
const ASR_ACTIVE_STATUSES = new Set(['queued', 'downloading', 'uploading', 'processing']);
function activeAutomaticTask() { return state.asr.tasks.find((task) => task.task_id === state.asr.activeTaskId) || null; }
function currentAsrResult() { const task = activeAutomaticTask(); return task ? task.result : state.asr.result; }
function asrTaskStageText(task) {
  if (task.cancel_requested) return '正在取消';
  const stage = String(task.stage || '');
  if (task.status !== 'processing' || !/^正在转写第 \d+\/\d+ 段/.test(stage)) return stage;
  const started = Date.parse(task.stage_started_at || '');
  const elapsed = Number.isFinite(started) ? Math.max(0, Math.floor((Date.now() - started) / 1000)) : 0;
  const attempt = Number(task.request_attempt || 0);
  return `${stage}${attempt > 1 ? ` · 第 ${attempt} 次请求` : ''} · 已等待 ${elapsed} 秒`;
}
function refreshAsrWaitLabels() {
  document.querySelectorAll('[data-asr-wait]').forEach((label) => {
    const task = { status: label.dataset.status, stage: label.dataset.stage, stage_started_at: label.dataset.startedAt, request_attempt: label.dataset.attempt, cancel_requested: label.dataset.cancelRequested === 'true' };
    label.textContent = asrTaskStageText(task);
  });
}
function renderAutomaticTasks() {
  const container = $('#asr-task-list'); if (!container) return;
  $('#asr-manage-toggle').classList.toggle('hidden', state.asr.manageMode);
  $('#asr-manage-actions').classList.toggle('hidden', !state.asr.manageMode);
  if (!state.asr.tasks.length) { container.innerHTML = '<div class="task-empty">暂无自动转写任务</div>'; return; }
  container.innerHTML = state.asr.tasks.map((task) => {
    const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
    const failed = ['failed', 'cancelled'].includes(task.status);
    const done = task.status === 'succeeded';
    const running = ASR_ACTIVE_STATUSES.has(task.status);
    const model = task.provider === 'local' ? '本地 FunASR' : task.model;
    const stage = asrTaskStageText(task);
    const checkbox = state.asr.manageMode ? `<input class="asr-task-select" type="checkbox" data-asr-select="${escapeHtml(task.task_id)}" aria-label="选择 ${escapeHtml(task.name || '转写任务')}" ${state.asr.selectedTaskIds.includes(task.task_id) ? 'checked' : ''} ${running ? 'disabled title="任务运行中，不能删除"' : ''}>` : '';
    const cancelButton = running ? `<button class="secondary asr-task-cancel" data-asr-cancel="${escapeHtml(task.task_id)}" ${task.cancel_requested ? 'disabled' : ''}>${task.cancel_requested ? '正在取消…' : '取消'}</button>` : '';
    return `<article class="asr-task ${task.task_id === state.asr.activeTaskId ? 'active' : ''} ${state.asr.manageMode ? 'managing' : ''}" data-asr-task="${escapeHtml(task.task_id)}"><div class="asr-task-main"><div class="task-top"><span class="task-name">${escapeHtml(task.name || task.source_title || '转写任务')}</span><span class="task-status ${failed ? 'failed' : done ? 'done' : ''}">${escapeHtml(AUTO_ASR_STATUS[task.status] || task.status || '等待中')}</span></div><div class="task-progress"><span style="width:${done ? 100 : progress}%"></span></div><div class="task-meta"><span>${escapeHtml(model || '')}</span><span data-asr-wait data-status="${escapeHtml(task.status || '')}" data-stage="${escapeHtml(task.stage || '')}" data-started-at="${escapeHtml(task.stage_started_at || '')}" data-attempt="${escapeHtml(task.request_attempt || 0)}" data-cancel-requested="${Boolean(task.cancel_requested)}">${escapeHtml(stage)}</span><span>进度：${done ? 100 : progress}%</span></div>${task.error ? `<div class="task-error">${escapeHtml(task.error)}</div>` : ''}</div>${cancelButton}${checkbox}</article>`;
  }).join('');
}
function renderAsr() {
  const asr = state.asr; const health = asr.health; const healthEl = $('#asr-health');
  healthEl.textContent = health?.message || '检查中'; healthEl.classList.toggle('ready', Boolean(health?.available)); healthEl.classList.toggle('problem', health?.mode === 'offline');
  renderAutomaticTasks();
  const info = $('#asr-file-info'); info.classList.toggle('hidden', !asr.file); if (asr.file) info.textContent = `${asr.file.name} · ${formatFileSize(asr.file.size)}${Number.isFinite(asr.duration) ? ` · ${formatAsrDuration(asr.duration)}` : ''}`;
  $('#asr-start').disabled = !asr.file || !health?.available || Boolean(asr.job && !['failed', 'cancelled', 'succeeded'].includes(asr.job.status)); $('#asr-new-file').classList.toggle('hidden', !asr.file);
  const automatic = activeAutomaticTask(); const job = automatic || asr.job; $('#asr-status-card').classList.toggle('hidden', !job); if (job) {
    const key = job.phase || job.status; const copy = ASR_STATUS[key] || ['转写状态未知', ''];
    const errorText = typeof job.error === 'string' ? job.error : job.error?.message;
    const hasRealProgress = Number.isFinite(job.progress);
    $('#asr-status-title').textContent = automatic ? (AUTO_ASR_STATUS[job.status] || '转写任务') : copy[0];
    $('#asr-status-detail').textContent = errorText || (hasRealProgress ? `${copy[1]} · 进度：${job.progress}%` : copy[1]);
    if (automatic) $('#asr-status-detail').textContent = errorText || `${asrTaskStageText(job)} · 进度：${job.progress}%`;
    $('#asr-cancel').classList.toggle('hidden', Boolean(automatic) || !['queued', 'processing'].includes(job.status));
    $('#asr-progress').classList.toggle('hidden', !hasRealProgress);
    $('#asr-progress-text').classList.toggle('hidden', !hasRealProgress);
    if (hasRealProgress) { $('#asr-progress-fill').style.width = `${job.progress}%`; $('#asr-progress-text').textContent = `已完成 ${job.progress}%`; }
    document.querySelectorAll('[data-asr-step]').forEach((step) => { const order = ['preparing', 'uploading', 'queued', 'processing', 'succeeded']; const activeIndex = order.indexOf(key); const ownIndex = order.indexOf(step.dataset.asrStep); step.classList.toggle('active', ownIndex <= activeIndex && key !== 'failed' && key !== 'cancelled'); });
  } else { $('#asr-progress').classList.add('hidden'); $('#asr-progress-text').classList.add('hidden'); }
  const result = currentAsrResult(); $('#asr-result').classList.toggle('hidden', !result); if (!result) return;
  const modelLabel = automatic ? (automatic.provider === 'local' ? '本地 FunASR' : automatic.model) : (health?.mode === 'mock' ? '模拟模式' : '本地 FunASR');
  $('#asr-result-meta').textContent = `${modelLabel} · ${automatic?.name || automatic?.filename || asrFileName()}`; $('#asr-text').textContent = result.text?.trim() || '该任务已完成，但没有可显示的文字稿。';
}
async function checkAsrHealth() {
  if (state.transcription.provider === 'local') {
    try { state.asr.health = await AsrClient.checkHealth(); } catch (_) { state.asr.health = { mode: 'offline', available: false, message: '本地转写服务未启动' }; }
  } else {
    const configured = Boolean(state.transcription.mimo_api_key_configured);
    state.asr.health = { mode: 'mimo', available: configured, message: configured ? `MiMo 已配置：${state.transcription.model}` : 'MiMo API Key 未配置' };
  }
  renderAsr();
}
async function refreshTranscriptionTasks() {
  try {
    const result = await api('/api/transcriptions');
    state.asr.tasks = (result.items || []).sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    if (state.asr.activeTaskId && !state.asr.tasks.some((task) => task.task_id === state.asr.activeTaskId)) { state.asr.activeTaskId = null; state.asr.result = null; }
    if (!state.asr.activeTaskId && state.asr.tasks.length) state.asr.activeTaskId = (state.asr.tasks.find((task) => task.status === 'succeeded') || state.asr.tasks[0]).task_id;
    renderAsr();
  } catch (_) { /* 本地服务轮询失败由顶部连接状态显示 */ }
}
async function setAsrFile(file) {
  asrMessage(''); if (!file) return; const extension = file.name.split('.').pop().toLowerCase();
  if (!ASR_EXTENSIONS.has(extension)) { asrMessage('请选择 M4A、MP3、WAV 或 FLAC 音频文件。'); $('#asr-file').value = ''; return; }
  if (file.size > ASR_MAX_FILE_BYTES) { asrMessage('音频文件超过 2 GB，暂不支持转写。'); $('#asr-file').value = ''; return; }
  state.asr.file = file; state.asr.duration = null; state.asr.job = null; state.asr.result = null; state.asr.activeTaskId = null;
  const audio = document.createElement('audio'); const url = URL.createObjectURL(file); audio.onloadedmetadata = () => { state.asr.duration = Number.isFinite(audio.duration) ? audio.duration : null; URL.revokeObjectURL(url); renderAsr(); }; audio.onerror = () => { URL.revokeObjectURL(url); renderAsr(); }; audio.src = url; renderAsr();
}
function stopAsrPolling() { if (state.asr.polling) clearInterval(state.asr.polling); state.asr.polling = null; }
async function pollAsrJob() { const job = state.asr.job; if (!job) return; try { state.asr.job = await AsrClient.getJob(job.task_id); if (state.asr.job.status === 'succeeded') { state.asr.result = await AsrClient.getJobResult(job.task_id); stopAsrPolling(); } if (['failed', 'cancelled'].includes(state.asr.job.status)) stopAsrPolling(); } catch (error) { state.asr.job = { ...job, status: 'failed', error: error.message }; stopAsrPolling(); asrMessage('转写服务暂时不可用。'); } renderAsr(); }
async function startAsr() {
  if (!state.asr.file) { asrMessage('请先选择本地音频文件。'); return; }
  asrMessage('');
  try {
    const form = new FormData(); form.append('file', state.asr.file, state.asr.file.name);
    const result = await uploadApi('/api/transcriptions/manual', form);
    state.asr.job = null; state.asr.result = null;
    renderAsr(); await refreshTranscriptionTasks();
  } catch (error) { asrMessage(error.message); renderAsr(); }
}
async function cancelAsr() { if (!state.asr.job) return; try { state.asr.job = await AsrClient.cancelJob(state.asr.job.task_id); stopAsrPolling(); } catch (error) { if (error.status === 409) asrMessage('任务已开始转写，当前不能中断。'); else asrMessage(error.message); } renderAsr(); }
async function cancelTranscriptionTask(taskId) {
  const task = state.asr.tasks.find((item) => item.task_id === taskId);
  if (!task || !ASR_ACTIVE_STATUSES.has(task.status) || task.cancel_requested) return;
  if (!window.confirm('确定取消此转写任务？已经提交给云端的当前片段可能仍产生费用，但后续片段会停止。')) return;
  task.cancel_requested = true; task.stage = '正在取消'; task.stage_started_at = new Date().toISOString(); renderAsr();
  try { await api(`/api/transcriptions/${encodeURIComponent(taskId)}/cancel`, { method: 'POST', body: '{}' }); await refreshTranscriptionTasks(); }
  catch (error) { await refreshTranscriptionTasks(); asrMessage(error.message); }
}
async function copyAsrText() { try { await navigator.clipboard.writeText(currentAsrResult()?.text || ''); asrMessage('全文已复制。'); } catch (_) { asrMessage('无法自动复制，请手动选择文字稿。'); } }
function exportAsrText() { const result = currentAsrResult(); if (!result) return; const blob = new Blob([result.text || ''], { type: 'text/plain;charset=utf-8' }); const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = `${activeAutomaticTask()?.name || asrFileName()}.txt`.replace(/[\\/:*?"<>|]/g, '_'); link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 0); }
function setAsrTaskSelected(taskId, selected) {
  const task = state.asr.tasks.find((item) => item.task_id === taskId);
  if (!task || ASR_ACTIVE_STATUSES.has(task.status)) return;
  const selection = new Set(state.asr.selectedTaskIds);
  if (selected) selection.add(taskId); else selection.delete(taskId);
  state.asr.selectedTaskIds = [...selection];
}
function toggleAsrManageMode(enabled) { state.asr.manageMode = enabled; state.asr.selectedTaskIds = []; renderAsr(); }
async function deleteSelectedTranscriptions() {
  const taskIds = [...state.asr.selectedTaskIds];
  if (!taskIds.length) { asrMessage('请先选择要删除的任务。'); return; }
  if (!window.confirm(`确定删除选中的 ${taskIds.length} 个转写任务及文字稿吗？`)) return;
  try {
    const result = await api('/api/transcriptions', { method: 'DELETE', body: JSON.stringify({ task_ids: taskIds }) });
    if (result.deleted?.includes(state.asr.activeTaskId)) { state.asr.activeTaskId = null; state.asr.result = null; }
    state.asr.selectedTaskIds = []; state.asr.manageMode = false; await refreshTranscriptionTasks();
    asrMessage(`已删除 ${result.deleted?.length || 0} 个任务${result.skipped?.length ? `，${result.skipped.length} 个运行中任务已保留` : ''}。`);
  } catch (error) { asrMessage(error.message); }
}
async function clearTranscriptionHistory() {
  const count = state.asr.tasks.filter((task) => !ASR_ACTIVE_STATUSES.has(task.status)).length;
  if (!count) { asrMessage('目前没有可清除的转写历史。'); return; }
  if (!window.confirm(`确定清除 ${count} 个已结束的转写任务和文字稿吗？运行中的任务会保留。`)) return;
  try {
    const result = await api('/api/transcriptions', { method: 'DELETE', body: JSON.stringify({ clear_all: true }) });
    if (result.deleted?.includes(state.asr.activeTaskId)) { state.asr.activeTaskId = null; state.asr.result = null; }
    state.asr.selectedTaskIds = []; state.asr.manageMode = false; await refreshTranscriptionTasks();
    asrMessage(`已清除 ${result.deleted?.length || 0} 个任务${result.skipped?.length ? `，${result.skipped.length} 个运行中任务已保留` : ''}。`);
  } catch (error) { asrMessage(error.message); }
}

function setupNavigation() {
  const pageDetails = {
    bilibili: ['B站课程', '解析公开视频、UP 主投稿，并可直接下载视频、音频或送入转写中心。'],
    meeting: ['腾讯会议回放', '处理你有权限访问的课程回放。'],
    asr: ['转写中心', '统一管理来自 B 站、腾讯会议和本地文件的转写任务。'],
    settings: ['设置', '管理存储位置、账号和 AI 转写服务。'],
  };
  document.querySelectorAll('.nav-button[data-page]').forEach((button) => button.addEventListener('click', () => {
    document.querySelectorAll('.nav-button[data-page]').forEach((item) => item.classList.remove('active'));
    document.querySelectorAll('.page').forEach((item) => item.classList.add('hidden'));
    button.classList.add('active');
    $(`#page-${button.dataset.page}`).classList.remove('hidden');
    const [title, description] = pageDetails[button.dataset.page] || pageDetails.bilibili;
    $('#page-title').textContent = title;
    $('#page-description').textContent = description;
    if (button.dataset.page === 'asr') checkAsrHealth();
  }));
  document.querySelectorAll('.section-nav-button[data-subpage]').forEach((button) => button.addEventListener('click', () => {
    const page = button.closest('.page');
    button.closest('.section-nav').querySelectorAll('.section-nav-button').forEach((item) => item.classList.toggle('active', item === button));
    page.querySelectorAll(':scope > .subpage').forEach((panel) => panel.classList.toggle('hidden', panel.id !== `page-${button.dataset.subpage}`));
  }));
}
function bindEvents() {
  $('#parse-button').addEventListener('click', parseVideo); $('#video-url').addEventListener('keydown', (event) => { if (event.key === 'Enter') parseVideo(); });
  $('#select-all').addEventListener('click', () => document.querySelectorAll('#page-list input').forEach((input) => input.checked = true));
  $('#clear-all').addEventListener('click', () => document.querySelectorAll('#page-list input').forEach((input) => input.checked = false));
  $('#download-button').addEventListener('click', createTask); $('#choose-first-dir').addEventListener('click', chooseDirectory); $('#choose-dir').addEventListener('click', chooseDirectory);
  $('#open-download-dir').addEventListener('click', () => simpleAction('/api/open-download-directory')); $('#open-logs').addEventListener('click', () => simpleAction('/api/open-log-directory')); $('#export-logs').addEventListener('click', () => simpleAction('/api/export-logs'));
  $('#login-start').addEventListener('click', startLogin); $('#login-logout').addEventListener('click', logout);
  $('#meeting-bridge-check').addEventListener('click', refreshMeetingStatus);
  $('#meeting-parse').addEventListener('click', parseMeetingRecording); $('#meeting-url').addEventListener('keydown', (event) => { if (event.key === 'Enter') parseMeetingRecording(); });
  $('#task-list').addEventListener('click', (event) => { const target = event.target; if (target.dataset.stop) stopTask(target.dataset.stop); if (target.dataset.retry) retryTask(target.dataset.retry); });
  $('#up-parse-button').addEventListener('click', parseUp); $('#up-url').addEventListener('keydown', (event) => { if (event.key === 'Enter') parseUp(); });
  $('#up-filter-button').addEventListener('click', applyUpFilter); $('#up-select-page').addEventListener('click', selectUpPage); $('#up-clear-page').addEventListener('click', clearUpPage); $('#up-clear-selected').addEventListener('click', clearUpSelected);
  $('#up-list').addEventListener('change', changeUpSelection); $('#up-detail-list').addEventListener('change', changeUpSelection);
  $('#up-download-button').addEventListener('click', () => startSelectedBatch(document.querySelector('input[name="up-mode"]:checked').value, 'up-download-button'));
  $('#up-detail-download-button').addEventListener('click', () => startSelectedBatch(document.querySelector('input[name="collection-mode"]:checked').value, 'up-detail-download-button'));
  $('#up-tab-posts').addEventListener('click', async () => { state.up.tab = 'posts'; state.up.detail = null; renderUp(); });
  $('#up-tab-collections').addEventListener('click', async () => { state.up.tab = 'collections'; state.up.detail = null; renderUp(); if (!state.up.collections.length) await loadCollections(true); });
  $('#up-collection-back').addEventListener('click', () => { state.up.tab = 'collections'; state.up.detail = null; renderUp(); });
  $('#up-detail-select-page').addEventListener('click', () => { (state.up.detail?.items || []).forEach((item) => { state.up.selected[item.bvid] = item; }); renderUp(); });
  $('#up-detail-clear-page').addEventListener('click', () => { (state.up.detail?.items || []).forEach((item) => delete state.up.selected[item.bvid]); renderUp(); });
  $('#up-clear-selected-detail').addEventListener('click', clearUpSelected);
  $('#asr-manage-toggle').addEventListener('click', () => toggleAsrManageMode(true)); $('#asr-manage-done').addEventListener('click', () => toggleAsrManageMode(false));
  $('#asr-clear-all').addEventListener('click', clearTranscriptionHistory); $('#asr-delete-selected').addEventListener('click', deleteSelectedTranscriptions);
  $('#asr-select-all').addEventListener('click', () => { state.asr.selectedTaskIds = state.asr.tasks.filter((task) => !ASR_ACTIVE_STATUSES.has(task.status)).map((task) => task.task_id); renderAsr(); });
  $('#asr-task-list').addEventListener('change', (event) => { const checkbox = event.target.closest('[data-asr-select]'); if (!checkbox) return; setAsrTaskSelected(checkbox.dataset.asrSelect, checkbox.checked); renderAsr(); });
  $('#asr-task-list').addEventListener('click', (event) => { const cancel = event.target.closest('[data-asr-cancel]'); if (cancel) { event.preventDefault(); event.stopPropagation(); cancelTranscriptionTask(cancel.dataset.asrCancel); return; } const task = event.target.closest('[data-asr-task]'); if (!task || state.asr.manageMode || event.target.closest('[data-asr-select]')) return; state.asr.activeTaskId = task.dataset.asrTask; renderAsr(); });
  $('#transcription-provider').addEventListener('change', () => { $('#mimo-key-row').classList.toggle('hidden', $('#transcription-provider').value === 'local'); }); $('#transcription-save').addEventListener('click', saveTranscriptionSettings);
  $('#asr-file').addEventListener('change', (event) => setAsrFile(event.target.files?.[0])); $('#asr-new-file').addEventListener('click', () => $('#asr-file').click()); $('#asr-start').addEventListener('click', startAsr); $('#asr-cancel').addEventListener('click', cancelAsr);
  $('#asr-copy').addEventListener('click', copyAsrText); $('#asr-export-txt').addEventListener('click', exportAsrText);
}
setupNavigation(); bindEvents(); renderAsr(); renderTranscriptionSettings(); refreshHealth(); refreshLoginStatus(); refreshMeetingStatus(); checkAsrHealth(); refreshTranscriptionSettings(); refreshTranscriptionTasks(); setInterval(refreshHealth, 3000); setInterval(refreshMeetingStatus, 2000); setInterval(refreshTasks, 1000); setInterval(refreshTranscriptionTasks, 1000); setInterval(refreshAsrWaitLabels, 1000);
