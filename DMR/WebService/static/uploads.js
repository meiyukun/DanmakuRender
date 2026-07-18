(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  let items = [];
  const itemCache = new Map();
  let parts = [];
  const partTitles = new Map();
  let defaults = {};
  let configuredSeasons = [];
  let seasonRequest = 0;
  const submissionCache = new Map();
  const submissionDetailCache = new Map();
  let submissionRequest = 0;
  let submissionDetailRequest = 0;
  let loadedSubmissionBvid = null;
  let libraryPage = 1;
  let libraryPages = 1;
  let libraryRequest = 0;
  let searchTimer = null;
  let coverToken = null;
  let referenceToken = null;
  let aiCoverAvailable = false;
  let currentPreviewPath = null;

  const labels = {
    src_video: '原始录屏', src_video_pre: '转码前录屏', dm_video: '弹幕版录屏',
    highlight_mix: '热点混剪', highlight_clip: '热点小片段', highlight_version: '自定义混剪',
  };
  const esc = value => {
    const div = document.createElement('div');
    div.textContent = value ?? '';
    return div.innerHTML;
  };
  const escAttr = value => esc(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const duration = seconds => {
    seconds = Math.max(0, Math.round(Number(seconds) || 0));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  };
  const size = bytes => bytes > 1073741824
    ? `${(bytes / 1073741824).toFixed(1)} GB`
    : `${(bytes / 1048576).toFixed(1)} MB`;
  const modified = value => value ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '时间未知';

  async function api(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || '请求失败');
    return data;
  }

  function toast(message, error = false) {
    $('upload-toast').textContent = message;
    $('upload-toast').style.borderColor = error ? '#8f4545' : '#287b69';
    $('upload-toast').hidden = false;
    setTimeout(() => { $('upload-toast').hidden = true; }, 4500);
  }

  function renderLibrary() {
    $('upload-library').innerHTML = items.map(item => `
      <div class="upload-media-item ${parts.includes(item.path) ? 'selected' : ''}" data-path="${escAttr(item.path)}">
        <div><strong>${esc(item.title)}</strong><br><small>${esc(item.group)} · ${item.duration ? duration(item.duration) : '时长未知'} · ${size(item.size)}</small><br><small>修改于 ${modified(item.modified_at)}</small></div>
        <div class="upload-media-actions"><span class="upload-kind">${labels[item.kind] || item.kind}</span><button type="button" class="upload-preview-button">预览</button></div>
      </div>`).join('') || '<div class="upload-empty">没有匹配的视频</div>';
  }

  function renderParts() {
    $('upload-part-count').textContent = parts.length;
    $('upload-submit-button').disabled = !parts.length;
    $('upload-parts').innerHTML = parts.map((path, index) => {
      const item = itemCache.get(path);
      return `<div class="upload-part" data-index="${index}">
        <span class="upload-part-index">P${index + 1}</span>
        <div class="upload-part-content"><input class="upload-part-title" maxlength="80" value="${escAttr(partTitles.get(path) || item?.part_title || item?.title || path)}" aria-label="P${index + 1} 分P名称"><small>素材：${esc(item?.title || path)} · ${duration(item?.duration)}</small></div>
        <div class="upload-part-actions">
          <button data-move="-1" ${index === 0 ? 'disabled' : ''}>↑</button>
          <button data-move="1" ${index === parts.length - 1 ? 'disabled' : ''}>↓</button>
          <button data-remove>×</button>
        </div>
      </div>`;
    }).join('') || '<div class="upload-empty">从左侧选择视频</div>';
    renderLibrary();
    renderCoverReferenceSources();
  }

  function renderCoverReferenceSources() {
    const select = $('upload-cover-reference-source');
    const current = select.value;
    select.innerHTML = '<option value="">不使用参考帧</option>' + parts.map(path => {
      const item = itemCache.get(path);
      return `<option value="${escAttr(path)}">${esc(item?.title || path)}</option>`;
    }).join('');
    if (parts.includes(current)) select.value = current;
  }

  function setCover(token, previewUrl, label) {
    coverToken = token;
    $('upload-cover-preview').src = `${previewUrl}&t=${Date.now()}`;
    $('upload-cover-result-label').textContent = label;
    $('upload-cover-result').hidden = false;
    updateCoverMode();
  }

  function clearCover() {
    coverToken = null;
    $('upload-cover-result').hidden = true;
    $('upload-cover-preview').removeAttribute('src');
    $('upload-cover-replace-confirmed').checked = false;
    updateCoverMode();
  }

  function updateCoverMode() {
    const coverMode = document.querySelector('input[name="upload-cover-mode"]:checked').value;
    $('upload-cover-file-panel').hidden = coverMode !== 'file';
    $('upload-cover-ai-panel').hidden = coverMode !== 'ai';
    $('upload-cover-prompt-button').disabled = coverMode !== 'ai' || !aiCoverAvailable || !parts.length;
    $('upload-cover-generate-button').disabled = coverMode !== 'ai' || !aiCoverAvailable ||
      !$('upload-cover-final-prompt').value.trim();
    const append = document.querySelector('input[name="upload-mode"]:checked').value === 'append';
    $('upload-cover-append-confirm').hidden = !(append && coverToken);
  }

  function resetGeneratedPrompt() {
    $('upload-cover-final-prompt').value = '';
    $('upload-cover-final-prompt-panel').hidden = true;
    updateCoverMode();
  }

  function toggle(path) {
    resetGeneratedPrompt();
    if (parts.includes(path)) {
      parts = parts.filter(value => value !== path);
      partTitles.delete(path);
    } else {
      parts = [...parts, path];
      const item = itemCache.get(path);
      partTitles.set(path, item?.part_title || item?.title || path);
    }
    renderParts();
  }

  function modeChanged() {
    const append = document.querySelector('input[name="upload-mode"]:checked').value === 'append';
    $('append-fields').hidden = !append;
    $('upload-metadata-status').textContent = append
      ? '选择稿件后会读取并保留原稿信息；保持原值不会改变原稿内容。'
      : '填写新投稿信息。';
    if (append) loadRecentSubmissions();
    updateCoverMode();
  }

  function seasonChanged() {
    $('upload-season-custom-field').hidden = $('upload-season').value !== '__custom__';
  }

  function renderSeasons(seasons, status = '') {
    const current = $('upload-season').value;
    $('upload-season').innerHTML = '<option value="">不加入合集</option>' +
      seasons.map(season => `<option value="${season.id}">${esc(season.name)}（${season.id}）</option>`).join('') +
      '<option value="__custom__">手动填写合集 ID…</option>';
    if ([...$('upload-season').options].some(option => option.value === current)) {
      $('upload-season').value = current;
    }
    $('upload-season-status').textContent = status;
    seasonChanged();
  }

  function renderLibraryFilters(data) {
    const selectedTask = $('upload-task-filter').value;
    $('upload-task-filter').innerHTML = '<option value="">全部任务</option>' +
      (data.tasknames || []).map(item => `<option value="${esc(item.name)}">${esc(item.name)}（${item.count}）</option>`).join('');
    if ([...$('upload-task-filter').options].some(option => option.value === selectedTask)) {
      $('upload-task-filter').value = selectedTask;
    }
    const selectedKind = $('upload-kind-filter').value;
    $('upload-kind-filter').innerHTML = '<option value="*">全部类型</option>' +
      (data.kinds || []).map(item => `<option value="${esc(item.kind)}">${labels[item.kind] || item.kind}（${item.count}）</option>`).join('');
    $('upload-kind-filter').value = [...$('upload-kind-filter').options].some(option => option.value === selectedKind)
      ? selectedKind : '*';
  }

  async function loadLibrary(resetPage = false) {
    if (resetPage) libraryPage = 1;
    const requestId = ++libraryRequest;
    const params = new URLSearchParams({
      page: libraryPage,
      page_size: $('upload-page-size').value,
      taskname: $('upload-task-filter').value,
      kind: $('upload-kind-filter').value,
      query: $('upload-search').value.trim(),
      sort: $('upload-sort').value,
    });
    $('upload-library').innerHTML = '<div class="upload-empty">正在加载当前页…</div>';
    $('upload-prev-page').disabled = true;
    $('upload-next-page').disabled = true;
    try {
      const data = await api(`/api/upload/library?${params}`);
      if (requestId !== libraryRequest) return;
      items = data.items || [];
      items.forEach(item => itemCache.set(item.path, item));
      libraryPage = data.page || 1;
      libraryPages = data.pages || 1;
      renderLibraryFilters(data);
      $('upload-library-total').textContent = `${data.total || 0} 个`;
      $('upload-page-status').textContent = `第 ${libraryPage} / ${libraryPages} 页`;
      $('upload-prev-page').disabled = libraryPage <= 1;
      $('upload-next-page').disabled = libraryPage >= libraryPages;
      renderLibrary();
      renderParts();
    } catch (error) {
      if (requestId !== libraryRequest) return;
      $('upload-library').innerHTML = `<div class="upload-empty">${esc(error.message)}</div>`;
      $('upload-library-total').textContent = '加载失败';
    }
  }

  async function loadOptions() {
    try {
      const data = await api('/api/upload/options');
      defaults = data.defaults || {};
      const accounts = data.accounts || [];
      $('upload-account').innerHTML = accounts.length
        ? accounts.map(account => `<option value="${esc(account)}">${esc(account)}</option>`).join('')
        : '<option value="">未发现可用账号</option>';
      const preferredAccount = defaults.account || 'bilibili';
      $('upload-account').value = accounts.includes(preferredAccount) ? preferredAccount : (accounts[0] || '');
      $('upload-title').value = defaults.title || '';
      $('upload-desc').value = defaults.desc || '';
      $('upload-dynamic').value = defaults.dynamic || '';
      $('upload-tag').value = defaults.tag || '';
      $('upload-tid').value = defaults.tid || 21;
      $('upload-source').value = defaults.source || '';
      configuredSeasons = data.seasons || [];
      aiCoverAvailable = Boolean(data.ai_cover?.available);
      $('upload-cover-ai-availability').textContent = aiCoverAvailable
        ? 'AI 接口已配置，可结合投稿信息、热点资料和参考帧生成封面。'
        : `AI 生图不可用：${data.ai_cover?.reason || '配置不完整'}`;
      updateCoverMode();
      renderSeasons(configuredSeasons, '选择账号后自动读取');
      loadAccountSeasons();
      loadRecentSubmissions();
    } catch (error) {
      $('upload-account').innerHTML = '<option value="">账号加载失败</option>';
      $('upload-season-status').textContent = error.message;
    }
  }

  async function loadAccountSeasons() {
    const account = $('upload-account').value;
    const requestId = ++seasonRequest;
    if (!account) {
      renderSeasons(configuredSeasons, '未发现可用的B站上传账号');
      return;
    }
    $('upload-season').disabled = true;
    $('upload-season-status').textContent = '正在读取账号合集…';
    try {
      const data = await api(`/api/upload/seasons?account=${encodeURIComponent(account)}`);
      if (requestId !== seasonRequest) return;
      renderSeasons(data.seasons || [], `已读取 ${data.seasons?.length || 0} 个合集`);
    } catch (error) {
      if (requestId !== seasonRequest) return;
      renderSeasons(configuredSeasons, `读取失败：${error.message}`);
    } finally {
      if (requestId === seasonRequest) $('upload-season').disabled = false;
    }
  }

  function applySubmissionDetail(detail) {
    $('upload-title').value = detail.title || '';
    $('upload-desc').value = detail.desc || '';
    $('upload-dynamic').value = detail.dynamic || '';
    $('upload-tag').value = detail.tag || '';
    $('upload-tid').value = detail.tid || 21;
    $('upload-copyright').value = String(detail.copyright || 1);
    $('upload-source').value = detail.source || '';
    $('upload-private').checked = Boolean(detail.is_only_self);
    $('upload-scheduled').value = detail.scheduled_at
      ? new Date(detail.scheduled_at * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)
      : '';
    const seasonId = detail.season_id ? String(detail.season_id) : '';
    if ([...$('upload-season').options].some(option => option.value === seasonId)) {
      $('upload-season').value = seasonId;
    } else if (seasonId) {
      $('upload-season').value = '__custom__';
      $('upload-season-custom').value = seasonId;
    } else {
      $('upload-season').value = '';
      $('upload-season-custom').value = '';
    }
    $('upload-section-title').value = detail.section_title || '';
    $('upload-episode-title').value = detail.episode_title || detail.title || '';
    seasonChanged();
  }

  async function loadSubmissionDetail(bvid) {
    const account = $('upload-account').value;
    bvid = String(bvid || '').trim();
    loadedSubmissionBvid = null;
    if (!account || !/^BV[a-zA-Z0-9]+$/.test(bvid)) {
      $('upload-submission-status').textContent = '请选择稿件或填写有效的 BV 号';
      return;
    }
    const requestId = ++submissionDetailRequest;
    const cacheKey = `${account}:${bvid.toLowerCase()}`;
    $('upload-submit-button').disabled = true;
    $('upload-submission-status').textContent = '正在读取稿件标题、简介、动态、标签、分区和合集信息…';
    try {
      let detail = submissionDetailCache.get(cacheKey);
      if (!detail) {
        const data = await api(`/api/upload/submission?account=${encodeURIComponent(account)}&bvid=${encodeURIComponent(bvid)}`);
        detail = data.submission;
        submissionDetailCache.set(cacheKey, detail);
      }
      if (requestId !== submissionDetailRequest) return;
      applySubmissionDetail(detail || {});
      loadedSubmissionBvid = detail.bvid || bvid;
      $('upload-submission-status').textContent = detail.season_id
        ? `已读取原稿全部可编辑信息；当前位于“${detail.season_title || detail.season_id}”合集`
        : '已读取原稿全部可编辑信息；当前未加入合集';
      $('upload-metadata-status').textContent = '已回填原稿信息。保持原值即不修改；可直接编辑标题、简介、动态、标签、分区及合集。';
    } catch (error) {
      if (requestId !== submissionDetailRequest) return;
      $('upload-submission-status').textContent = `读取稿件详情失败：${error.message}`;
      toast(error.message, true);
    } finally {
      if (requestId === submissionDetailRequest) $('upload-submit-button').disabled = !parts.length;
    }
  }

  function submissionChanged() {
    const value = $('upload-submission').value;
    $('upload-bvid-custom-field').hidden = value !== '__custom__';
    loadedSubmissionBvid = null;
    if (value && value !== '__custom__') loadSubmissionDetail(value);
  }

  function renderSubmissions(submissions, status = '') {
    const current = $('upload-submission').value;
    $('upload-submission').innerHTML = '<option value="">请选择已有稿件</option>' +
      submissions.map(item => `<option value="${esc(item.bvid)}">${esc(item.title)} · ${esc(item.bvid)}${item.published_at ? ` · ${modified(item.published_at)}` : ''}</option>`).join('') +
      '<option value="__custom__">手动填写 BV 号…</option>';
    if ([...$('upload-submission').options].some(option => option.value === current)) {
      $('upload-submission').value = current;
    }
    $('upload-submission-status').textContent = status;
    submissionChanged();
  }

  async function loadRecentSubmissions() {
    if (document.querySelector('input[name="upload-mode"]:checked').value !== 'append') return;
    const account = $('upload-account').value;
    const requestId = ++submissionRequest;
    if (!account) {
      renderSubmissions([], '未发现可用的B站上传账号');
      $('upload-submission').value = '__custom__';
      submissionChanged();
      $('upload-submission').disabled = false;
      return;
    }
    if (submissionCache.has(account)) {
      const cached = submissionCache.get(account);
      renderSubmissions(cached, `已读取 ${cached.length} 个最近投稿`);
      $('upload-submission').disabled = false;
      return;
    }
    $('upload-submission').disabled = true;
    $('upload-submission-status').textContent = '正在读取最近投稿…';
    try {
      const data = await api(`/api/upload/submissions?account=${encodeURIComponent(account)}`);
      if (requestId !== submissionRequest) return;
      const submissions = data.submissions || [];
      submissionCache.set(account, submissions);
      renderSubmissions(submissions, `已读取 ${submissions.length} 个最近投稿`);
    } catch (error) {
      if (requestId !== submissionRequest) return;
      renderSubmissions([], `读取失败：${error.message}`);
      $('upload-submission').value = '__custom__';
      submissionChanged();
    } finally {
      if (requestId === submissionRequest) $('upload-submission').disabled = false;
    }
  }

  function closePreview() {
    const video = $('upload-preview-video');
    video.pause();
    video.removeAttribute('src');
    video.load();
    $('upload-preview-modal').hidden = true;
    currentPreviewPath = null;
  }

  function openPreview(path) {
    const item = itemCache.get(path);
    const url = `/api/upload/media?path=${encodeURIComponent(path)}`;
    $('upload-preview-title').textContent = item?.title || '视频预览';
    $('upload-preview-status').textContent = '正在读取视频元数据…';
    $('upload-preview-open').href = url;
    $('upload-preview-modal').hidden = false;
    currentPreviewPath = path;
    const video = $('upload-preview-video');
    video.src = url;
    video.load();
    video.play().catch(() => {});
  }

  async function uploadCoverFile() {
    const file = $('upload-cover-file').files[0];
    if (!file) return;
    const body = new FormData();
    body.append('cover', file);
    $('upload-cover-result-label').textContent = '正在处理上传图片…';
    try {
      const data = await api('/api/upload/cover/file', {method: 'POST', body});
      setCover(data.token, data.preview_url, '手动上传封面');
    } catch (error) {
      clearCover();
      toast(error.message, true);
    }
  }

  async function captureReference(path = null, second = null) {
    path = path || $('upload-cover-reference-source').value;
    second = second ?? Number($('upload-cover-reference-second').value);
    if (!path || !parts.includes(path)) {
      toast('请先从待上传分P中选择参考视频', true);
      return;
    }
    $('upload-cover-generate-status').textContent = '正在截取参考帧…';
    try {
      const data = await api('/api/upload/cover/reference', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path, second}),
      });
      referenceToken = data.token;
      resetGeneratedPrompt();
      $('upload-cover-reference-source').value = path;
      $('upload-cover-reference-second').value = Number(data.second).toFixed(1);
      $('upload-cover-reference-preview').src = `${data.preview_url}&t=${Date.now()}`;
      $('upload-cover-reference-preview').hidden = false;
      $('upload-cover-generate-status').textContent = '参考帧已就绪';
    } catch (error) {
      $('upload-cover-generate-status').textContent = error.message;
      toast(error.message, true);
    }
  }

  async function pollCoverJob(jobId, jobType) {
    try {
      const job = await api(`/api/upload/cover/generate/status?job_id=${encodeURIComponent(jobId)}`);
      const labels = {queued: '等待生成…', analyzing: '正在整理投稿和热点信息…', generating: '正在生成封面…'};
      $('upload-cover-generate-status').textContent = labels[job.status] || job.error || job.status;
      if (job.status === 'completed' && jobType === 'prompt') {
        if (job.metadata) {
          $('upload-title').value = job.metadata.title || '';
          $('upload-desc').value = job.metadata.desc || '';
          $('upload-dynamic').value = job.metadata.dynamic || '';
        }
        $('upload-cover-final-prompt').value = job.prompt || '';
        $('upload-cover-final-prompt-panel').hidden = false;
        $('upload-cover-prompt-button').disabled = false;
        $('upload-cover-generate-status').textContent = '标题、简介、动态和封面提示词已生成，可编辑后确认';
        updateCoverMode();
        return;
      }
      if (job.status === 'completed') {
        setCover(job.token, job.preview_url, 'AI 生成封面');
        $('upload-cover-generate-button').disabled = false;
        $('upload-cover-generate-status').textContent = '生成完成；可继续编辑提示词并重生成';
        return;
      }
      if (job.status === 'failed') {
        $('upload-cover-prompt-button').disabled = false;
        $('upload-cover-generate-button').disabled = false;
        toast(job.error || 'AI 封面生成失败', true);
        return;
      }
      setTimeout(() => pollCoverJob(jobId, jobType), 1200);
    } catch (error) {
      $('upload-cover-prompt-button').disabled = false;
      $('upload-cover-generate-button').disabled = false;
      $('upload-cover-generate-status').textContent = error.message;
    }
  }

  async function prepareCoverPrompt() {
    if (!parts.length) return toast('请先选择待上传视频', true);
    $('upload-cover-prompt-button').disabled = true;
    $('upload-cover-generate-status').textContent = '正在一次生成标题、简介、动态和封面提示词…';
    try {
      const data = await api('/api/upload/cover/prompt', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          paths: parts, title: $('upload-title').value, desc: $('upload-desc').value,
          dynamic: $('upload-dynamic').value,
          prompt: $('upload-cover-prompt').value, reference_token: referenceToken,
        }),
      });
      pollCoverJob(data.job_id, 'prompt');
    } catch (error) {
      $('upload-cover-prompt-button').disabled = false;
      $('upload-cover-generate-status').textContent = error.message;
      toast(error.message, true);
    }
  }

  async function generateCover() {
    const confirmedPrompt = $('upload-cover-final-prompt').value.trim();
    if (!confirmedPrompt) return toast('请先生成并确认完整生图提示词', true);
    $('upload-cover-generate-button').disabled = true;
    $('upload-cover-generate-status').textContent = '正在创建生图任务…';
    try {
      const data = await api('/api/upload/cover/generate', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          paths: parts, confirmed_prompt: confirmedPrompt, reference_token: referenceToken,
        }),
      });
      pollCoverJob(data.job_id, 'image');
    } catch (error) {
      $('upload-cover-generate-button').disabled = false;
      $('upload-cover-generate-status').textContent = error.message;
      toast(error.message, true);
    }
  }

  async function submit() {
    const mode = document.querySelector('input[name="upload-mode"]:checked').value;
    const payload = {
      parts: parts.map(path => ({path, title: (partTitles.get(path) || '').trim()})),
      mode,
      bvid: $('upload-submission').value === '__custom__'
        ? $('upload-bvid').value : $('upload-submission').value,
      submission_loaded_bvid: loadedSubmissionBvid,
      insert_head: $('upload-insert-head').checked,
      title: $('upload-title').value,
      desc: $('upload-desc').value,
      dynamic: $('upload-dynamic').value,
      tag: $('upload-tag').value,
      tid: Number($('upload-tid').value),
      copyright: Number($('upload-copyright').value),
      source: $('upload-source').value,
      account: $('upload-account').value,
      is_only_self: $('upload-private').checked,
      season_id: $('upload-season').value === '__custom__'
        ? $('upload-season-custom').value : $('upload-season').value,
      section_title: $('upload-section-title').value,
      episode_title: $('upload-episode-title').value,
      scheduled_at: $('upload-scheduled').value ? new Date($('upload-scheduled').value).getTime() / 1000 : 0,
      cover_token: coverToken,
      cover_replace_confirmed: $('upload-cover-replace-confirmed').checked,
    };
    if (mode === 'append' && coverToken && !payload.cover_replace_confirmed) {
      return toast('请确认替换整个已有稿件的封面', true);
    }
    const coverNotice = coverToken ? (mode === 'append' ? '，并替换原稿封面' : '，并使用所选封面') : '';
    if (!confirm(`确定将 ${parts.length} 个视频作为${mode === 'new' ? '新投稿' : '追加分P'}提交${coverNotice}吗？`)) return;
    $('upload-submit-button').disabled = true;
    $('upload-submit-status').textContent = '正在提交…';
    try {
      const data = await api('/api/upload/submit', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
      });
      $('upload-submit-status').textContent = `已提交 ${data.part_count} 个分P`;
      toast('上传任务已进入队列，可在任务页查看进度');
      parts = [];
      partTitles.clear();
      referenceToken = null;
      clearCover();
      resetGeneratedPrompt();
      renderParts();
    } catch (error) {
      $('upload-submit-status').textContent = error.message;
      toast(error.message, true);
    } finally {
      $('upload-submit-button').disabled = !parts.length;
    }
  }

  document.addEventListener('click', event => {
    const preview = event.target.closest('.upload-preview-button');
    if (preview) return openPreview(preview.closest('.upload-media-item').dataset.path);
    const media = event.target.closest('.upload-media-item');
    if (media) return toggle(media.dataset.path);
    const part = event.target.closest('.upload-part');
    if (!part) return;
    const index = Number(part.dataset.index);
    if (event.target.closest('[data-remove]')) {
      resetGeneratedPrompt();
      partTitles.delete(parts[index]);
      parts.splice(index, 1);
    } else {
      const move = event.target.closest('[data-move]');
      if (!move) return;
      resetGeneratedPrompt();
      const target = index + Number(move.dataset.move);
      [parts[index], parts[target]] = [parts[target], parts[index]];
    }
    renderParts();
  });
  document.addEventListener('input', event => {
    if (!event.target.matches('.upload-part-title')) return;
    const index = Number(event.target.closest('.upload-part').dataset.index);
    if (parts[index]) partTitles.set(parts[index], event.target.value);
  });
  document.querySelectorAll('input[name="upload-mode"]').forEach(element =>
    element.addEventListener('change', modeChanged));
  document.querySelectorAll('input[name="upload-cover-mode"]').forEach(element =>
    element.addEventListener('change', () => {
      clearCover();
      resetGeneratedPrompt();
      updateCoverMode();
    }));
  $('upload-task-filter').addEventListener('change', () => {
    $('upload-kind-filter').value = '*';
    loadLibrary(true);
  });
  $('upload-kind-filter').addEventListener('change', () => loadLibrary(true));
  $('upload-sort').addEventListener('change', () => loadLibrary(true));
  $('upload-page-size').addEventListener('change', () => loadLibrary(true));
  $('upload-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadLibrary(true), 300);
  });
  $('upload-prev-page').addEventListener('click', () => { if (libraryPage > 1) { libraryPage--; loadLibrary(); } });
  $('upload-next-page').addEventListener('click', () => { if (libraryPage < libraryPages) { libraryPage++; loadLibrary(); } });
  $('upload-account').addEventListener('change', () => {
    loadedSubmissionBvid = null;
    $('upload-submission').value = '';
    loadAccountSeasons();
    loadRecentSubmissions();
  });
  $('upload-season').addEventListener('change', seasonChanged);
  $('upload-submission').addEventListener('change', submissionChanged);
  $('upload-bvid').addEventListener('change', event => loadSubmissionDetail(event.target.value));
  $('upload-preview-close').addEventListener('click', closePreview);
  $('upload-preview-reference').addEventListener('click', () => {
    const video = $('upload-preview-video');
    const path = currentPreviewPath;
    const second = Number(video.currentTime || 0);
    closePreview();
    if (!parts.includes(path)) return toast('只有已加入分P列表的视频才能作为参考源', true);
    document.querySelector('input[name="upload-cover-mode"][value="ai"]').checked = true;
    updateCoverMode();
    captureReference(path, second);
  });
  $('upload-preview-modal').addEventListener('click', event => {
    if (event.target === $('upload-preview-modal')) closePreview();
  });
  $('upload-preview-video').addEventListener('loadedmetadata', () => {
    $('upload-preview-status').textContent = '预览已就绪；播放和拖动进度条时按需读取文件。';
  });
  $('upload-preview-video').addEventListener('error', () => {
    $('upload-preview-status').textContent = '当前浏览器无法直接播放这种封装或编码，可尝试在新窗口打开原文件。';
  });
  $('upload-clear-parts').addEventListener('click', () => { parts = []; partTitles.clear(); renderParts(); });
  $('upload-cover-file').addEventListener('change', uploadCoverFile);
  $('upload-cover-reference-button').addEventListener('click', () => captureReference());
  $('upload-cover-reference-source').addEventListener('change', () => {
    referenceToken = null;
    $('upload-cover-reference-preview').hidden = true;
    resetGeneratedPrompt();
  });
  $('upload-cover-prompt').addEventListener('input', resetGeneratedPrompt);
  $('upload-cover-final-prompt').addEventListener('input', updateCoverMode);
  $('upload-cover-prompt-button').addEventListener('click', prepareCoverPrompt);
  $('upload-cover-generate-button').addEventListener('click', generateCover);
  $('upload-cover-clear').addEventListener('click', clearCover);
  $('upload-submit-button').addEventListener('click', submit);

  loadOptions();
  loadLibrary();
  modeChanged();
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !$('upload-preview-modal').hidden) closePreview();
  });
})();
