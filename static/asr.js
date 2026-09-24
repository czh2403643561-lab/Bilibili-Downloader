/* 本地 ASR 客户端边界：真实服务接入时只需替换此文件中的 Adapter。 */
window.AsrClient = (() => {
  const ASR_API_BASE_URL = ''; // 真实服务交付后只在此处配置，不猜测端口。
  const VALID_STATUS = new Set(['queued', 'processing', 'succeeded', 'failed', 'cancelled']);
  const mockJobs = new Map();
  let mockFailNext = false;
  const now = () => new Date().toISOString();

  /** @typedef {{task_id:string,status:'queued'|'processing'|'succeeded'|'failed'|'cancelled',progress:number|null,created_at:string,started_at:string|null,finished_at:string|null,error:string|null,phase?:'preparing'|'uploading'}} AsrJob */
  /** @typedef {{type:'local_file'|'bilibili',file?:File,bvid?:string}} AsrSource */
  /** @typedef {{start_ms:number,end_ms:number,text:string,speaker:string|null}} AsrSegment */
  /** @typedef {{api_version:string,task_id:string,status:string,text:string,segments:AsrSegment[],timestamps:boolean,model:string|null,metrics:object}} AsrResult */

  function normalizeJob(raw) {
    if (!raw || typeof raw.task_id !== 'string' || !VALID_STATUS.has(raw.status)) throw new Error('转写服务返回的任务结构不正确。');
    return { task_id: raw.task_id, status: raw.status, progress: Number.isFinite(raw.progress) ? raw.progress : null, created_at: raw.created_at || now(), started_at: raw.started_at || null, finished_at: raw.finished_at || null, error: raw.error || null, phase: raw.phase };
  }
  function validSegment(segment) { return Number.isFinite(segment?.start_ms) && Number.isFinite(segment?.end_ms) && segment.start_ms >= 0 && segment.end_ms > segment.start_ms; }
  function normalizeResult(raw) {
    if (!raw || !Array.isArray(raw.segments) || typeof raw.text !== 'string') throw new Error('转写服务返回的文字稿结构不正确。');
    return { api_version: raw.api_version || 'unknown', task_id: String(raw.task_id || ''), status: raw.status || 'succeeded', text: raw.text, segments: raw.segments.map((item) => ({ start_ms: Number(item.start_ms), end_ms: Number(item.end_ms), text: String(item.text || ''), speaker: item.speaker == null ? null : String(item.speaker) })), timestamps: Boolean(raw.timestamps), model: raw.model || null, metrics: raw.metrics || {} };
  }
  function mockResult(taskId) {
    const segments = [{ start_ms: 0, end_ms: 4200, text: '这是第一段测试文字。', speaker: null }, { start_ms: 4200, end_ms: 9100, text: '这是第二段测试文字。', speaker: null }];
    return normalizeResult({ api_version: 'mock-1', task_id: taskId, status: 'succeeded', text: segments.map((item) => item.text).join('\n'), segments, timestamps: true, model: null, metrics: { mode: 'mock' } });
  }
  function advanceMock(job) {
    const elapsed = Date.now() - job.created_ms;
    if (job.status === 'cancelled' || job.status === 'failed' || job.status === 'succeeded') return job;
    if (elapsed < 550) job.phase = 'preparing';
    else if (elapsed < 1100) job.phase = 'uploading';
    else if (elapsed < 1650) { delete job.phase; job.status = 'queued'; }
    else if (elapsed < 2500) { job.status = 'processing'; job.started_at ||= now(); }
    else if (job.fail) { job.status = 'failed'; job.finished_at = now(); job.error = '模拟转写失败。'; }
    else { job.status = 'succeeded'; job.finished_at = now(); job.result = mockResult(job.task_id); }
    return job;
  }
  async function request(path, options = {}) {
    const response = await fetch(`${ASR_API_BASE_URL}${path}`, options);
    let data = {}; try { data = await response.json(); } catch (_) { /* 使用统一中文错误 */ }
    if (!response.ok) throw new Error(data.error || '本地转写服务请求失败。');
    return data;
  }
  async function checkHealth() {
    if (!ASR_API_BASE_URL) return { mode: 'mock', available: true, message: '模拟模式 / 等待真实 ASR 服务' };
    try { await request('/health'); return { mode: 'service', available: true, message: 'ASR 服务正常' }; }
    catch (_) { return { mode: 'offline', available: false, message: '本地转写服务未启动' }; }
  }
  async function createTranscriptionJob(source) {
    if (!source?.file || source.type !== 'local_file') throw new Error('请先选择本地音频文件。');
    if (ASR_API_BASE_URL) throw new Error('真实 ASR 服务适配器尚未配置上传格式。');
    const task_id = `mock-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    const job = { task_id, status: 'queued', progress: null, created_at: now(), started_at: null, finished_at: null, error: null, phase: 'preparing', created_ms: Date.now(), result: null, fail: mockFailNext };
    mockFailNext = false;
    mockJobs.set(task_id, job); return normalizeJob(job);
  }
  async function getJob(taskId) {
    if (ASR_API_BASE_URL) return normalizeJob(await request(`/v1/jobs/${encodeURIComponent(taskId)}`));
    const job = mockJobs.get(taskId); if (!job) throw new Error('转写任务不存在，服务可能已重启。'); return normalizeJob(advanceMock(job));
  }
  async function getJobResult(taskId) {
    if (ASR_API_BASE_URL) return normalizeResult(await request(`/v1/jobs/${encodeURIComponent(taskId)}/result`));
    const job = mockJobs.get(taskId); if (!job) throw new Error('转写任务不存在，服务可能已重启。'); advanceMock(job); if (job.status !== 'succeeded') throw new Error('文字稿尚未准备完成。'); return job.result;
  }
  async function cancelJob(taskId) {
    if (ASR_API_BASE_URL) return normalizeJob(await request(`/v1/jobs/${encodeURIComponent(taskId)}`, { method: 'DELETE' }));
    const job = mockJobs.get(taskId); if (!job) throw new Error('转写任务不存在，服务可能已重启。'); advanceMock(job); if (job.status === 'queued' || job.status === 'processing') { job.status = 'cancelled'; job.finished_at = now(); } return normalizeJob(job);
  }
  const pad = (value, size = 2) => String(value).padStart(size, '0');
  function timestamp(ms, vtt = false) { const value = Math.floor(ms); const h = Math.floor(value / 3600000); const m = Math.floor(value % 3600000 / 60000); const s = Math.floor(value % 60000 / 1000); const milli = value % 1000; return `${pad(h)}:${pad(m)}:${pad(s)}${vtt ? '.' : ','}${pad(milli, 3)}`; }
  function subtitle(result, kind) { const valid = result.segments.filter(validSegment); if (!valid.length || valid.length !== result.segments.length) throw new Error('文字稿中存在异常时间，无法导出字幕。'); const blocks = valid.map((item, index) => `${kind === 'srt' ? `${index + 1}\n` : ''}${timestamp(item.start_ms, kind === 'vtt')} --> ${timestamp(item.end_ms, kind === 'vtt')}\n${item.text}`); return `${kind === 'vtt' ? 'WEBVTT\n\n' : ''}${blocks.join('\n\n')}\n`; }
  function exportText(result, kind) { if (kind === 'txt') return result.text; return subtitle(result, kind); }
  return { ASR_API_BASE_URL, checkHealth, createTranscriptionJob, getJob, getJobResult, cancelJob, normalizeJob, normalizeResult, validSegment, exportText, setMockFailureForTest: (enabled = true) => { mockFailNext = Boolean(enabled); } };
})();
