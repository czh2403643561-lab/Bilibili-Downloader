const state = { video: null, online: false, stale: false, restarting: false, directorySelecting: false, taskOptions: JSON.parse(localStorage.getItem('bbdown-task-options') || '{}'), up: { input: '', profile: null, items: [], selected: {}, page: 1, total: 0, totalPages: 0, period: 'all', keyword: '', loading: false, tab: 'posts', collections: [], collectionPage: 1, collectionTotal: 0, collectionTotalPages: 0, collectionLoading: false, postsError: '', collectionsError: '', detail: null } };
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
async function startSelectedBatch(mode, buttonId) { const selected = Object.values(state.up.selected); if (!selected.length) { $('#up-message').textContent = '请至少选择一个视频。'; return; } const button = $(`#${buttonId}`); button.disabled = true; let added = 0; for (const item of selected) { try { const task = await api('/api/tasks', { method: 'POST', body: JSON.stringify({ url: item.url, pages: [], all_pages: true, mode }) }); state.taskOptions[task.id] = { url: item.url, pages: [], all_pages: true, mode }; added += 1; } catch (error) { $('#up-message').textContent = `已加入 ${added} 个，部分任务失败：${error.message}`; break; } } localStorage.setItem('bbdown-task-options', JSON.stringify(state.taskOptions)); if (added === selected.length) $('#up-message').textContent = `已加入 ${added} 个下载任务。`; await refreshTasks(); button.disabled = false; renderUp(); }

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

function setupNavigation() { document.querySelectorAll('.nav-button').forEach((button) => button.addEventListener('click', () => { document.querySelectorAll('.nav-button').forEach((item) => item.classList.remove('active')); document.querySelectorAll('.page').forEach((item) => item.classList.add('hidden')); button.classList.add('active'); $(`#page-${button.dataset.page}`).classList.remove('hidden'); $('#page-title').textContent = ({ single: '单视频下载', up: 'UP 主批量下载', settings: '设置' }[button.dataset.page]); })); }
function bindEvents() {
  $('#parse-button').addEventListener('click', parseVideo); $('#video-url').addEventListener('keydown', (event) => { if (event.key === 'Enter') parseVideo(); });
  $('#select-all').addEventListener('click', () => document.querySelectorAll('#page-list input').forEach((input) => input.checked = true));
  $('#clear-all').addEventListener('click', () => document.querySelectorAll('#page-list input').forEach((input) => input.checked = false));
  $('#download-button').addEventListener('click', createTask); $('#choose-first-dir').addEventListener('click', chooseDirectory); $('#choose-dir').addEventListener('click', chooseDirectory);
  $('#open-download-dir').addEventListener('click', () => simpleAction('/api/open-download-directory')); $('#open-logs').addEventListener('click', () => simpleAction('/api/open-log-directory')); $('#export-logs').addEventListener('click', () => simpleAction('/api/export-logs'));
  $('#login-start').addEventListener('click', startLogin); $('#login-logout').addEventListener('click', logout);
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
}
setupNavigation(); bindEvents(); refreshHealth(); refreshLoginStatus(); setInterval(refreshHealth, 3000); setInterval(refreshTasks, 1000);
