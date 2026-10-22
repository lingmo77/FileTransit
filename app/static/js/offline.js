/* 离线下载：所有任务与操作意图保存在服务器，页面只负责显示。 */
(function () {
  'use strict';

  var app = document.getElementById('offline-app');
  if (!app || !window.FT) return;

  var endpoint = '/api/offline/tasks';
  var tasks = [];
  var page = 1;
  var pages = 1;
  var filter = 'all';
  var enabled = app.dataset.enabled === 'true';
  var loading = false;
  var timer = null;
  var modal = null;
  var operationPending = false;
  var maxDays = Math.max(1, Number(app.dataset.maxExpireDays) || 30);
  var metadataMaxMb = Number(app.dataset.metadataMaxMb) || 5;
  var newButton = document.getElementById('offline-new');
  var list = document.getElementById('offline-tasks');
  var empty = document.getElementById('offline-empty');
  var serviceBox = document.getElementById('offline-service');
  var serviceText = document.getElementById('offline-service-text');

  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function duration(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '计算中';
    if (seconds < 60) return Math.ceil(seconds) + ' 秒';
    if (seconds < 3600) return Math.ceil(seconds / 60) + ' 分钟';
    if (seconds < 86400) return Math.floor(seconds / 3600) + ' 小时 ' + Math.ceil((seconds % 3600) / 60) + ' 分';
    return Math.floor(seconds / 86400) + ' 天 ' + Math.ceil((seconds % 86400) / 3600) + ' 小时';
  }

  function saved(task) {
    return !!task.imported_at || ['saved', 'complete', 'completed', 'seeding'].indexOf(task.state) !== -1;
  }

  function seeding(task) {
    return task.state === 'seeding' || (saved(task) && (task.transfers || []).some(function (transfer) {
      return ['active', 'seeding'].indexOf(transfer.state) !== -1 && transfer.role !== 'metadata';
    }));
  }

  function cancelled(task) {
    return ['cancelled', 'canceled', 'removed'].indexOf(task.state) !== -1 || task.desired_state === 'cancelled';
  }

  function needsSelection(task) {
    return ['awaiting_selection', 'waiting_selection', 'selecting', 'selection'].indexOf(task.state) !== -1;
  }

  function stateLabel(task) {
    if (seeding(task)) return ['已保存 · 做种中', 'badge-success'];
    if (saved(task)) return ['已保存', 'badge-success'];
    if (cancelled(task)) return ['已取消', ''];
    if (needsSelection(task)) return ['等待选择文件', 'badge-primary'];
    if (task.desired_state === 'paused' || task.state === 'paused') return ['已暂停', ''];
    var labels = {
      created: '等待调度', queued: '排队中', pending: '等待调度',
      metadata: '解析资源中', resolving: '解析资源中',
      awaiting_selection: '等待选择文件', waiting_selection: '等待选择文件', selecting: '等待选择文件', selection: '等待选择文件',
      downloading: '下载中', active: '下载中',
      importing: '正在存入网盘', saving: '正在存入网盘',
      waiting_quota: '等待可用配额', waiting_space: '等待可用空间',
      quota_wait: '等待可用配额或空间',
      resource_wait: '相同资源排队中', waiting_resource: '相同资源排队中',
      retrying: '正在重试连接', cancelling: '正在取消',
      error: '任务失败', failed: '任务失败', save_error: '保存失败'
    };
    var failure = ['error', 'failed', 'save_error'].indexOf(task.state) !== -1;
    return [labels[task.state] || '等待状态更新', failure ? 'badge-danger' : 'badge-primary'];
  }

  function button(action, label, task) {
    return '<button type="button" class="btn btn-sm" data-task-action="' + action + '" data-task-id="' + escapeHtml(task.id) + '"' + (operationPending ? ' disabled' : '') + '>' + label + '</button>';
  }

  function renderTask(task) {
    var label = stateLabel(task);
    var total = Math.max(0, Number(task.total_bytes) || 0);
    var done = Math.max(0, Number(task.completed_bytes) || 0);
    var speed = Math.max(0, Number(task.download_speed) || 0);
    var percent = saved(task) ? 100 : total ? Math.min(100, Math.floor(done * 100 / total)) : 0;
    var kind = { http: '文件直链', https: '文件直链', magnet: '磁力链接', torrent: 'BT 种子' }[task.kind] || '离线下载';
    var transferText = FT.formatSize(done) + (total ? ' / ' + FT.formatSize(total) + ' · ' + percent + '%' : ' · 总大小待确认');
    var rateText = '下载 ' + FT.formatSize(speed) + '/s';
    if (task.kind !== 'http' && task.kind !== 'https') rateText += ' · 上传 ' + FT.formatSize(Math.max(0, Number(task.upload_speed) || 0)) + '/s';
    if (speed > 0 && total > done && !saved(task)) rateText += ' · 剩余 ' + duration((total - done) / speed);
    if (Number(task.connections) > 0 && !saved(task)) rateText += ' · ' + Number(task.connections) + ' 个连接';
    var controls = '';
    if (needsSelection(task)) controls += button('select', '选择文件', task);
    else if (!saved(task) && !cancelled(task) && ['error', 'failed', 'save_error'].indexOf(task.state) === -1) {
      controls += task.state === 'paused' || task.desired_state === 'paused' ? button('resume', '继续', task) : button('pause', '暂停', task);
    }
    if (['error', 'failed', 'save_error', 'waiting_quota', 'waiting_space', 'quota_wait'].indexOf(task.state) !== -1) controls += button('retry', task.state === 'save_error' ? '重试保存' : '重试', task);
    if (!saved(task) && !cancelled(task) && task.state !== 'cancelling') controls += button('cancel', '取消', task);
    if (seeding(task)) controls += button('cancel', '停止做种', task);
    if (saved(task)) controls += button('view', '查看文件', task);
    if (saved(task) || cancelled(task) || ['error', 'failed', 'save_error'].indexOf(task.state) !== -1) controls += button('remove', '移除记录', task);
    var privacy = task.is_public ? '公开分享' : '仅自己可见';
    var retention = Number(task.expires_hours) === 0 ? '长期有效' : Number(task.expires_hours) % 24 === 0 ? Number(task.expires_hours) / 24 + ' 天' : Number(task.expires_hours) + ' 小时';
    return '<article class="card offline-task" data-task-card="' + escapeHtml(task.id) + '">' +
      '<div class="offline-task-heading"><div class="grow"><h2 class="offline-task-name">' + escapeHtml(task.name || '正在解析文件名…') + '</h2><div class="offline-task-meta tiny muted">' + kind + ' · ' + privacy + ' · 保留 ' + retention + '</div></div><span class="badge ' + label[1] + '">' + label[0] + '</span></div>' +
      (!cancelled(task) && !needsSelection(task) ? '<div class="meter offline-progress" role="progressbar" aria-label="下载进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + percent + '"><i style="width:' + percent + '%"></i></div>' : '') +
      '<div class="offline-task-transfer tiny muted"><span>' + escapeHtml(transferText) + '</span><span>' + escapeHtml(rateText) + '</span></div>' +
      (task.error ? '<p class="offline-task-error small">' + escapeHtml(task.error) + '</p>' : '') +
      (task.imported_at ? '<div class="tiny muted offline-task-saved">保存于 ' + escapeHtml(FT.formatTime(task.imported_at)) + '</div>' : '') +
      '<div class="offline-task-actions row">' + controls + '</div></article>';
  }

  function renderTasks(data) {
    tasks = data.tasks || [];
    page = Math.max(1, Number(data.page) || 1);
    pages = Math.max(1, Number(data.pages) || 1);
    list.innerHTML = tasks.map(renderTask).join('');
    empty.hidden = tasks.length > 0;
    document.getElementById('offline-empty-text').textContent = filter === 'all' ? '还没有离线下载任务' : '没有此状态的任务';
    document.getElementById('offline-count').textContent = '共 ' + (Number(data.total) || 0) + ' 个任务';
    document.getElementById('offline-pagination').hidden = pages <= 1;
    document.getElementById('offline-page-label').textContent = page + ' / ' + pages;
    document.getElementById('offline-prev').disabled = page <= 1;
    document.getElementById('offline-next').disabled = page >= pages;
    if (data.max_expire_days) maxDays = Math.max(1, Number(data.max_expire_days));
    if (data.metadata_max_mb) metadataMaxMb = Math.max(1, Number(data.metadata_max_mb));
  }

  function serviceStatus(data) {
    var service = data.service || {};
    if (typeof service.enabled === 'boolean') enabled = service.enabled;
    else if (typeof data.enabled === 'boolean') enabled = data.enabled;
    newButton.disabled = !enabled;
    serviceBox.classList.remove('alert-warning', 'alert-success');
    if (!enabled) {
      serviceText.textContent = '离线下载已关闭，请联系管理员开启。已有任务记录仍可查看。';
      serviceBox.dataset.status = 'disabled';
    } else if (service.available === false) {
      serviceText.textContent = service.error || '下载服务暂时不可用，正在等待恢复。';
      serviceBox.classList.add('alert-warning');
      serviceBox.dataset.status = 'waiting';
    } else {
      serviceText.textContent = service.config_synced === false ? '下载服务已连接，后台设置正在同步。' : '下载服务已连接，任务状态自动更新。';
      serviceBox.classList.add('alert-success');
      serviceBox.dataset.status = 'connected';
    }
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, document.hidden ? 12000 : 2000);
  }

  async function refresh() {
    if (loading || operationPending) { schedule(); return; }
    loading = true;
    try {
      var data = await FT.request(endpoint + '?page=' + page + '&state=' + encodeURIComponent(filter));
      renderTasks(data || {});
      serviceStatus(data || {});
    } catch (error) {
      serviceBox.dataset.status = 'waiting';
      serviceBox.classList.remove('alert-success');
      serviceBox.classList.add('alert-warning');
      serviceText.textContent = navigator.onLine === false ? '网络已断开。页面保留当前列表，恢复连接后自动更新。' : '暂时无法更新任务状态，正在重试连接。';
    } finally {
      loading = false;
      schedule();
    }
  }

  function openDialog(backdrop) {
    if (modal) modal.close();
    var previous = document.activeElement;
    var backgrounds = Array.prototype.map.call(document.querySelectorAll('body > header, body > main, body > footer'), function (node) {
      var old = node.inert;
      node.inert = true;
      return [node, old];
    });
    document.body.appendChild(backdrop);
    function close() {
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
      backgrounds.forEach(function (pair) { pair[0].inert = pair[1]; });
      if (previous && previous.isConnected) previous.focus();
      if (modal && modal.root === backdrop) modal = null;
    }
    function onKey(event) {
      if (event.key === 'Escape' && !backdrop.dataset.submitting) { event.preventDefault(); close(); }
      if (event.key !== 'Tab') return;
      var focusable = Array.prototype.filter.call(backdrop.querySelectorAll('button, input, textarea, select, a[href]'), function (el) { return !el.disabled && el.getClientRects().length; });
      if (!focusable.length) return;
      var first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    backdrop.addEventListener('click', function (event) {
      if ((event.target === backdrop || event.target.closest('[data-close]')) && !backdrop.dataset.submitting) close();
    });
    document.addEventListener('keydown', onKey);
    modal = { root: backdrop, close: close };
    var firstInput = backdrop.querySelector('textarea, input, select, button');
    if (firstInput) firstInput.focus();
    return modal;
  }

  function retentionOptions(select, hint) {
    var maximum = maxDays * 24;
    var defaultHours = Math.min(336, maximum);
    [[1, '1 小时'], [6, '6 小时'], [24, '1 天'], [72, '3 天'], [168, '7 天'], [336, '14 天'], [720, '30 天']].forEach(function (pair) {
      if (pair[0] <= maximum) select.add(new Option(pair[1], pair[0], false, pair[0] === defaultHours));
    });
    if (!Array.prototype.some.call(select.options, function (option) { return Number(option.value) === defaultHours; })) select.add(new Option(defaultHours / 24 + ' 天', defaultHours, true, true));
    if (!Array.prototype.some.call(select.options, function (option) { return Number(option.value) === maximum; })) select.add(new Option(maxDays + ' 天（站点上限）', maximum));
    if (app.dataset.canForever === 'true') select.add(new Option('长期有效（永久保存）', 0));
    if (maximum < 336) { hint.textContent = '当前站点最多保留 ' + maxDays + ' 天，已按站点上限设置。'; hint.hidden = false; }
    select.value = String(defaultHours);
  }

  function requestKey() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  }

  function createDialog() {
    var backdrop = document.getElementById('offline-create-template').content.firstElementChild.cloneNode(true);
    var dialog = openDialog(backdrop);
    var form = backdrop.querySelector('form');
    var source = 'link';
    var key = requestKey();
    form.addEventListener('input', function () { key = requestKey(); });
    retentionOptions(form.elements.expires_hours, form.querySelector('[data-retention-hint]'));
    form.querySelector('[data-torrent-hint]').textContent = '选择 .torrent 文件，最大 ' + metadataMaxMb + ' MB；解析后可选择下载内容';
    backdrop.querySelectorAll('[data-input-tab]').forEach(function (tab) {
      tab.addEventListener('click', function () {
        source = tab.dataset.inputTab;
        backdrop.querySelectorAll('[data-input-tab]').forEach(function (item) { item.classList.toggle('active', item === tab); item.setAttribute('aria-selected', item === tab ? 'true' : 'false'); });
        backdrop.querySelector('#offline-link-panel').hidden = source !== 'link';
        backdrop.querySelector('#offline-torrent-panel').hidden = source !== 'torrent';
      });
      tab.addEventListener('keydown', function (event) {
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        event.preventDefault();
        var other = backdrop.querySelector('[data-input-tab="' + (tab.dataset.inputTab === 'link' ? 'torrent' : 'link') + '"]');
        other.click();
        other.focus();
      });
    });
    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      var errorBox = form.querySelector('[data-form-error]');
      var results = form.querySelector('[data-create-results]');
      errorBox.hidden = true;
      results.hidden = true;
      var urls = form.elements.urls.value.split(/\r?\n/).map(function (line) { return line.trim(); }).filter(Boolean);
      var torrent = form.elements.torrent.files[0];
      if (source === 'link' && !urls.length) { errorBox.textContent = '请填写下载链接。'; errorBox.hidden = false; return; }
      if (source === 'link' && urls.length > 20) { errorBox.textContent = '每次最多添加 20 个链接。'; errorBox.hidden = false; return; }
      if (source === 'torrent' && !torrent) { errorBox.textContent = '请选择 BT 种子文件。'; errorBox.hidden = false; return; }
      if (source === 'torrent' && torrent.size > metadataMaxMb * 1024 * 1024) { errorBox.textContent = '种子文件超过 ' + metadataMaxMb + ' MB 上限。'; errorBox.hidden = false; return; }
      var submit = form.querySelector('[type="submit"]');
      var restore = FT.busy(submit, true);
      backdrop.dataset.submitting = 'true';
      operationPending = true;
      try {
        var data;
        if (source === 'torrent') {
          var payload = new FormData();
          payload.append('file', torrent);
          payload.append('expires_hours', form.elements.expires_hours.value);
          payload.append('is_public', String(form.elements.is_public.checked));
          payload.append('request_key', key);
          data = await FT.request(endpoint + '/torrent', { method: 'POST', body: {}, form: payload });
        } else {
          data = await FT.request(endpoint, { method: 'POST', body: { urls: urls, expires_hours: Number(form.elements.expires_hours.value), is_public: form.elements.is_public.checked, request_key: key } });
        }
        var errors = (data && data.errors) || [];
        var added = (data && data.tasks) || (data && data.task ? [data.task] : []);
        if (errors.length) {
          var failed = [];
          errors.forEach(function (error) {
            if (urls[error.index] !== undefined) failed.push(urls[error.index]);
          });
          form.elements.urls.value = failed.join('\n');
          results.hidden = false;
          results.innerHTML = '<p>已创建 ' + added.length + ' 个任务，以下链接未能添加：</p><ul>' + errors.map(function (error) { return '<li>第 ' + (Number(error.index) + 1) + ' 行：' + escapeHtml(error.error || '创建失败') + '</li>'; }).join('') + '</ul>';
          key = requestKey();
        } else {
          dialog.close();
          FT.toast('已创建 ' + Math.max(1, added.length) + ' 个任务', 'success');
        }
        filter = 'all'; page = 1;
        updateFilterButtons();
      } catch (error) {
        errorBox.textContent = error.message || '创建任务失败，请稍后再试。';
        errorBox.hidden = false;
      } finally {
        delete backdrop.dataset.submitting;
        operationPending = false;
        restore();
        refresh();
      }
    });
  }

  async function fileDialog(task, selecting) {
    var backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop offline-modal';
    backdrop.innerHTML = '<section class="modal card offline-file-modal" role="dialog" aria-modal="true" aria-labelledby="offline-files-title"><div class="card-head"><h2 id="offline-files-title"></h2><button type="button" class="icon-btn" data-close aria-label="关闭">×</button></div><div class="card-body"><p class="small muted" data-files-note></p><div class="row offline-file-tools"' + (selecting ? '' : ' hidden') + '><button type="button" class="btn btn-sm" data-select-all>全选</button><button type="button" class="btn btn-sm" data-select-none>全不选</button><span class="small muted" data-selection-summary></span></div><div class="offline-files-list"></div><div class="offline-form-error alert alert-danger" role="alert" hidden></div></div><div class="card-foot row" style="justify-content:flex-end"><button type="button" class="btn" data-close>关闭</button>' + (selecting ? '<button type="button" class="btn btn-primary" data-save-selection>确认下载</button>' : '') + '</div></section>';
    backdrop.querySelector('h2').textContent = selecting ? '选择下载文件' : '已保存的文件';
    backdrop.querySelector('[data-files-note]').textContent = selecting ? '只保存所选文件；文件数量和总大小需符合剩余配额。' : '点击文件名查看网盘文件。已删除或过期的文件不会重新保存。';
    var dialog = openDialog(backdrop);
    var files = task.files || [];
    var fileList = backdrop.querySelector('.offline-files-list');
    if (selecting) {
      fileList.innerHTML = files.map(function (file) {
        return '<label class="offline-file-row"><input type="checkbox" name="offline-selected" value="' + Number(file.index) + '" data-size="' + Math.max(0, Number(file.size) || 0) + '"' + (file.selected !== false ? ' checked' : '') + ' /><span class="grow offline-file-path">' + escapeHtml(file.relative_path || file.name) + '</span><span class="small muted">' + FT.formatSize(file.size) + '</span></label>';
      }).join('');
    } else {
      fileList.innerHTML = files.map(function (file) {
        var url = file.public_id ? '/f/' + encodeURIComponent(file.public_id) : '';
        var available = file.file_id && url;
        return '<div class="offline-file-row"><span class="grow offline-file-path">' + (available ? '<a href="' + url + '">' + escapeHtml(file.relative_path || file.name) + '</a>' : escapeHtml(file.relative_path || file.name)) + '</span><span class="small muted">' + (available ? FT.formatSize(file.size) : file.imported_at ? '已删除或已过期' : '尚未保存') + '</span></div>';
      }).join('') || '<p class="small muted">暂无可查看的文件。</p>';
    }
    function selectedSummary() {
      var selected = Array.prototype.filter.call(fileList.querySelectorAll('input'), function (input) { return input.checked; });
      var total = selected.reduce(function (sum, input) { return sum + Number(input.dataset.size); }, 0);
      backdrop.querySelector('[data-selection-summary]').textContent = selected.length + ' 个文件 · ' + FT.formatSize(total);
    }
    if (selecting) {
      selectedSummary();
      fileList.addEventListener('change', selectedSummary);
      backdrop.querySelector('[data-select-all]').addEventListener('click', function () { fileList.querySelectorAll('input').forEach(function (input) { input.checked = true; }); selectedSummary(); });
      backdrop.querySelector('[data-select-none]').addEventListener('click', function () { fileList.querySelectorAll('input').forEach(function (input) { input.checked = false; }); selectedSummary(); });
      backdrop.querySelector('[data-save-selection]').addEventListener('click', async function (event) {
        var indexes = Array.prototype.filter.call(fileList.querySelectorAll('input'), function (input) { return input.checked; }).map(function (input) { return Number(input.value); });
        var errorBox = backdrop.querySelector('.offline-form-error');
        if (!indexes.length) { errorBox.textContent = '请至少选择一个文件。'; errorBox.hidden = false; return; }
        var restore = FT.busy(event.currentTarget, true);
        backdrop.dataset.submitting = 'true';
        operationPending = true;
        try {
          await FT.request(endpoint + '/' + encodeURIComponent(task.id) + '/select', { method: 'POST', body: { indexes: indexes } });
          dialog.close(); FT.toast('已确认下载文件，任务将进入队列', 'success');
        } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
        finally { delete backdrop.dataset.submitting; operationPending = false; restore(); refresh(); }
      });
    }
  }

  async function taskAction(buttonElement) {
    if (operationPending) return;
    var id = buttonElement.dataset.taskId;
    var action = buttonElement.dataset.taskAction;
    var task = tasks.find(function (item) { return item.id === id; });
    if (!task) return;
    if (action === 'select' || action === 'view') {
      var restoreDetails = FT.busy(buttonElement, true);
      try {
        var details = await FT.request(endpoint + '/' + encodeURIComponent(id));
        await fileDialog((details && details.task) || task, action === 'select');
      } catch (error) { FT.toast(error.message, 'error'); }
      finally { restoreDetails(); }
      return;
    }
    if (action === 'cancel' || action === 'remove') {
      var stoppingSeed = action === 'cancel' && seeding(task);
      var text = stoppingSeed ? '停止此任务做种并清理暂存源副本？已存入网盘的文件会保留。' : action === 'cancel' ? '取消「' + (task.name || '此任务') + '」并清理尚未保存的下载内容？已存入网盘的文件会保留。' : '移除此任务记录？如仍在做种，会先停止做种并清理暂存内容。已存入网盘的文件会保留。';
      var confirmed = await FT.confirm(text, { title: stoppingSeed ? '停止做种' : action === 'cancel' ? '取消任务' : '移除任务记录', okText: stoppingSeed ? '停止做种' : action === 'cancel' ? '取消任务' : '移除记录' });
      if (!confirmed) return;
    }
    var restore = FT.busy(buttonElement, true);
    operationPending = true;
    try {
      await FT.request(endpoint + '/' + encodeURIComponent(id) + '/action', { method: 'POST', body: { action: action } });
      FT.toast({pause:'暂停请求已提交',resume:'继续请求已提交',cancel:'取消请求已提交',retry:'重试请求已提交',remove:'移除请求已提交'}[action] || '操作已提交', 'success');
    } catch (error) { FT.toast(error.message, 'error'); }
    finally { operationPending = false; restore(); refresh(); }
  }

  function updateFilterButtons() {
    app.querySelectorAll('[data-offline-filter]').forEach(function (button) {
      var active = button.dataset.offlineFilter === filter;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }

  app.addEventListener('click', function (event) {
    var actionButton = event.target.closest('[data-task-action]');
    if (actionButton) { taskAction(actionButton); return; }
    var filterButton = event.target.closest('[data-offline-filter]');
    if (filterButton) { filter = filterButton.dataset.offlineFilter; page = 1; updateFilterButtons(); refresh(); }
  });
  newButton.addEventListener('click', createDialog);
  document.getElementById('offline-refresh').addEventListener('click', refresh);
  document.getElementById('offline-prev').addEventListener('click', function () { if (page > 1) { page -= 1; refresh(); } });
  document.getElementById('offline-next').addEventListener('click', function () { if (page < pages) { page += 1; refresh(); } });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); else schedule(); });
  window.addEventListener('online', refresh);
  window.addEventListener('offline', function () { serviceText.textContent = '网络已断开。页面保留当前列表，恢复连接后自动更新。'; serviceBox.dataset.status = 'waiting'; });
  refresh();
})();
