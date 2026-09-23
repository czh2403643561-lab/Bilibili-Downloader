const state = { video: null, online: false, directorySelecting: false, taskOptions: JSON.parse(localStorage.getItem('bbdown-task-options') || '{}') };
const $ = (selector) => document.querySelector(selector);
const PLACEHOLDER_COVER = `data:image/svg+xml;charset=utf-8,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="328" height="184" viewBox="0 0 328 184"><rect width="328" height="184" fill="#eef0f4"/><path d="M132 78h64v28h-64z" fill="#c5ccd7"/><circle cx="148" cy="88" r="5" fill="#eef0f4"/><path d="m139 101 15-14 10 9 11-12 14 17z" fill="#eef0f4"/><text x="164" y="132" text-anchor="middle" fill="#8d98a8" font-size="14">封面暂时无法加载</text></svg>')}`;

async function api(path, options = {}) {
  let response;
  const { timeoutMs = 5000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timeout = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try { response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, signal: controller.signal, ...fetchOptions }); }
  catch (_) { if (!state.directorySelecting) setOffline(); throw new Error('本地服务已断开，请重新启动工具。'); }
  finally { if (timeout) clearTimeout(timeout); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || '请求失败，请稍后重试。');
  return data;
}

function formatDuration(seconds) { const m = Math.floor(seconds / 60); const s = seconds % 60; return `${m}:${String(s).padStart(2, '0')}`; }
function formatSpeed(speed) { if (!speed) return '速度：等待 BBDownNext 更新'; const units = ['B/s', 'KB/s', 'MB/s', 'GB/s']; let index = 0; while (speed >= 1024 && index < units.length - 1) { speed /= 1024; index++; } return `速度：${speed.toFixed(index ? 1 : 0)} ${units[index]}`; }
function escapeHtml(value) { const element = document.createElement('span'); element.textContent = value || ''; return element.innerHTML; }
function titleForStatus(task) { if (task.status === 'Finished') { if (task.isCancelled) return '已取消'; return task.isSuccessful ? '已完成' : '下载失败'; } return ({ Pending: '等待开始', Queued: '排队中', Running: '下载中' }[task.status] || task.status || '等待中'); }

function setServiceState(health) {
  const badge = $('#service-state');
  const problem = health.dependency_problem;
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

function setupNavigation() { document.querySelectorAll('.nav-button').forEach((button) => button.addEventListener('click', () => { document.querySelectorAll('.nav-button').forEach((item) => item.classList.remove('active')); document.querySelectorAll('.page').forEach((item) => item.classList.add('hidden')); button.classList.add('active'); $(`#page-${button.dataset.page}`).classList.remove('hidden'); $('#page-title').textContent = ({ single: '单视频下载', up: 'UP 主批量下载', settings: '设置' }[button.dataset.page]); })); }
function bindEvents() {
  $('#parse-button').addEventListener('click', parseVideo); $('#video-url').addEventListener('keydown', (event) => { if (event.key === 'Enter') parseVideo(); });
  $('#select-all').addEventListener('click', () => document.querySelectorAll('#page-list input').forEach((input) => input.checked = true));
  $('#clear-all').addEventListener('click', () => document.querySelectorAll('#page-list input').forEach((input) => input.checked = false));
  $('#download-button').addEventListener('click', createTask); $('#choose-first-dir').addEventListener('click', chooseDirectory); $('#choose-dir').addEventListener('click', chooseDirectory);
  $('#open-download-dir').addEventListener('click', () => simpleAction('/api/open-download-directory')); $('#open-logs').addEventListener('click', () => simpleAction('/api/open-log-directory')); $('#export-logs').addEventListener('click', () => simpleAction('/api/export-logs'));
  $('#task-list').addEventListener('click', (event) => { const target = event.target; if (target.dataset.stop) stopTask(target.dataset.stop); if (target.dataset.retry) retryTask(target.dataset.retry); });
}
setupNavigation(); bindEvents(); refreshHealth(); setInterval(refreshHealth, 3000); setInterval(refreshTasks, 1000);
