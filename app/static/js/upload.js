/* 上传面板 + 我的文件管理（拖拽上传、进度、删除、修改有效期） */
(function () {
  'use strict';

  var dropzone = document.getElementById('dropzone');
  if (!dropzone) return;

  var input = document.getElementById('file-input');
  var queue = document.getElementById('upload-queue');
  var expiresSelect = document.getElementById('expires_hours');
  var publicCheckbox = document.getElementById('is_public');
  var maxExpireDays = parseInt(dropzone.dataset.maxExpireDays || '30', 10);
  // 永久保存的权限由后台按用户发放；管理员天然具备
  var canForever = dropzone.dataset.canForever === 'true';
  var active = 0;
  var uploadedSomething = false;

  var EXPIRY_OPTIONS = [
    [1, '1 小时'],
    [6, '6 小时'],
    [24, '1 天'],
    [72, '3 天'],
    [168, '7 天'],
    [336, '14 天'],
    [720, '30 天'],
  ];

  // 0 是「长期有效」的哨兵值，只有获得授权的用户能选（后端也会再拦一次）
  if (canForever) EXPIRY_OPTIONS.push([0, '长期有效']);

  /* ---------------------------------------------------------- 选择文件 */

  function pick() {
    input.click();
  }

  dropzone.addEventListener('click', pick);
  dropzone.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      pick();
    }
  });

  ['pick-files', 'pick-files-empty'].forEach(function (id) {
    var button = document.getElementById(id);
    if (button) button.addEventListener('click', pick);
  });

  ['dragenter', 'dragover'].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault();
      dropzone.classList.add('dragging');
    });
  });

  ['dragleave', 'dragend', 'drop'].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault();
      dropzone.classList.remove('dragging');
    });
  });

  dropzone.addEventListener('drop', function (event) {
    var files = event.dataTransfer && event.dataTransfer.files;
    if (files && files.length) uploadAll(files);
  });

  input.addEventListener('change', function () {
    if (input.files && input.files.length) uploadAll(input.files);
    input.value = '';
  });

  // 支持直接粘贴文件
  document.addEventListener('paste', function (event) {
    if (event.target && /input|textarea/i.test(event.target.tagName)) return;
    var items = event.clipboardData && event.clipboardData.files;
    if (items && items.length) uploadAll(items);
  });

  /* ---------------------------------------------------------- 队列渲染 */

  var DISMISS_ICON =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M18 6 6 18M6 6l12 12"/></svg>';

  // 和模板里的 icon('refresh') 是同一个图形，用同一套线宽和线帽
  var RETRY_ICON =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<polyline points="23 4 23 10 17 10"/>' +
    '<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';

  /** 把秒数写成人话：45 秒 / 6 分 12 秒 / 1 小时 5 分 */
  function formatDuration(seconds) {
    if (!isFinite(seconds) || seconds <= 0) return '';
    var total = Math.round(seconds);
    if (total < 60) return total + ' 秒';
    var minutes = Math.floor(total / 60);
    var rest = total % 60;
    if (minutes < 60) return rest ? minutes + ' 分 ' + rest + ' 秒' : minutes + ' 分';
    return Math.floor(minutes / 60) + ' 小时 ' + (minutes % 60) + ' 分';
  }

  function createRow(file) {
    var el = document.createElement('div');
    el.className = 'upload-item';
    el.dataset.fileName = file.name;
    el.innerHTML =
      '<div class="name"></div>' +
      '<div class="tiny muted status">等待中</div>' +
      '<div class="upload-actions">' +
      '<button type="button" class="icon-btn upload-retry" title="继续上传" ' +
      'aria-label="继续上传" hidden>' +
      RETRY_ICON +
      '</button>' +
      '<button type="button" class="icon-btn upload-dismiss" title="取消上传" ' +
      'aria-label="取消上传">' +
      DISMISS_ICON +
      '</button>' +
      '</div>' +
      '<div class="progress"><i></i></div>';

    el.querySelector('.name').textContent = file.name;
    var status = el.querySelector('.status');
    var bar = el.querySelector('.progress > i');
    var dismiss = el.querySelector('.upload-dismiss');
    var retry = el.querySelector('.upload-retry');
    var uploading = false;

    function paint(text) {
      status.textContent = text;
    }

    /** 收尾后按钮从「取消上传」变成「从列表移除」 */
    function settle(className, text) {
      uploading = false;
      el.classList.add(className);
      paint(text);
      dismiss.title = '从列表移除';
      dismiss.setAttribute('aria-label', '从列表移除');
    }

    return {
      el: el,
      dismiss: dismiss,
      retry: retry,

      message: paint,
      isUploading: function () {
        return uploading;
      },

      /** 发起一次尝试前把这一行抹干净（第一次和重试都走这里） */
      begin: function () {
        uploading = true;
        el.classList.remove('done', 'failed', 'cancelled');
        retry.hidden = true;
        dismiss.title = '暂停上传';
        dismiss.setAttribute('aria-label', '暂停上传');
        paint('等待中');
        // 先关掉过渡再归零：不然重试时进度条是从 100% 倒着缩回去的。
        // 中间读一次 offsetWidth 强制结算，让「归零」这一步先落地——
        // 换成 requestAnimationFrame 的话，赶在下一帧之前的进度事件会
        // 在过渡被关掉的状态下更新，那一下就不走动画了。
        bar.style.transition = 'none';
        bar.style.width = '0%';
        void bar.offsetWidth;
        bar.style.transition = '';
      },

      /** 上传中：百分比 + 实时速度 + 预计剩余时间 */
      progress: function (ratio, speed, loaded, total) {
        // 又能收到进度事件了：说明刚才只是慢，不是卡死，把重试收回去
        retry.hidden = true;

        var percent = Math.round(ratio * 100) + '%';
        bar.style.width = Math.round(ratio * 100) + '%';

        if (total > 0 && loaded >= total) {
          // 字节已经发完，剩下的是服务端落盘 + 校验，这段时间没有进度事件，
          // 显示速度只会停在最后一个采样值，看着像卡住了
          paint(percent + ' · 服务器处理中…');
          return;
        }

        var text = percent;
        if (speed > 0) {
          text += ' · ' + FT.formatSize(speed) + '/s';
          var left = (total - loaded) / speed;
          var human = formatDuration(left);
          if (human) text += ' · 剩余 ' + human;
        }
        paint(text);
      },

      /** 超过 2 秒没有收到进度事件：别再让读数停在一个骗人的值上，并把重试露出来 */
      stalled: function (percent) {
        paint(percent + ' · 已停顿，可重试…');
        retry.hidden = false;
      },

      done: function () {
        bar.style.width = '100%';
        retry.hidden = true;
        settle('done', '已完成');
      },
      fail: function (message) {
        settle('failed', message || '失败');
        retry.hidden = false;
      },
      cancelled: function () {
        settle('cancelled', '已暂停，已完成的分片保留 ' + (dropzone.dataset.uploadTtlHours || '24') + ' 小时');
        retry.hidden = false;
      },
    };
  }

  /* 页面重载会掐断所有在传的 XHR——队列会因此集体变成「已取消」。
     所以凡是「改完想刷新」的地方都登记到这里，等上传队列空了再真刷。 */
  var reload = { wanted: false, delay: 900, timer: null };

  function refresh(delay) {
    if (delay !== undefined) reload.delay = delay;
    reload.wanted = true;
    if (reload.timer || active > 0) return; // 还有上传在跑，等它结束再来兑现
    reload.wanted = false;
    reload.timer = setTimeout(function () {
      window.location.reload();
    }, reload.delay);
  }

  function uploadAll(fileList) {
    Array.prototype.forEach.call(fileList, uploadOne);
  }

  /* 列表被前端摘掉一张卡片之后，标题里的计数和空状态得当场跟着变。
     不能只靠 refresh() 那次整页刷新兜底：它要等上传队列空了才兑现，队列里只要
     还挂着东西（比如卡住没结束的那次上传），它就永远排不上队，计数会一直停在
     旧值上。这里的更新和模板渲染的结果是一致的：两个块都渲染、只切显隐。
     页边界的情况（删掉第 2 页最后一张卡片、第 1 页还有文件）不动空状态——
     总数为 0 才说明是真空了，这时候说「还没有上传过文件」才是真话。 */
  function syncListAfterRemoval() {
    var grid = document.getElementById('file-grid');
    var empty = document.getElementById('file-grid-empty');
    var count = document.getElementById('share-count');
    if (!grid || !count) return;

    var total = Math.max(0, parseInt(count.dataset.total || '0', 10) - 1);
    count.dataset.total = String(total);
    count.textContent = '（' + total + '）';

    if (total === 0) {
      grid.hidden = true;
      if (empty) empty.hidden = false;
    }
  }

  /* 顶部那两张配额卡（已用文件数 / 已用空间）和标题计数一样，数字只由服务端
     渲染。删掉文件后不自己改，队列里只要卡着一次上传，那次延后的整页刷新就
     永远排不上队，数字会一直停在旧值上——和计数是同一个坑。

     卡片上带 data-used / data-max，前端只减自己这一份，不去猜。max 为 -1 表示
     无限制（后端 quota 的哨兵值），此时进度条宽度和模板一样恒为 0。 */
  function syncQuotaAfterRemoval(size) {
    bumpStat('stat-files', 'stat-files-used', -1, false, function (used) {
      return String(used);
    });
    bumpStat('stat-bytes', 'stat-bytes-used', -(size || 0), true, function (used) {
      return FT.formatSize(used);
    });
  }

  function bumpStat(cardId, valueId, delta, warnable, format) {
    var card = document.getElementById(cardId);
    if (!card) return;

    var used = Math.max(0, (parseInt(card.dataset.used, 10) || 0) + delta);
    card.dataset.used = String(used);

    var value = document.getElementById(valueId);
    if (value) value.textContent = format(used);

    var meter = card.querySelector('.meter');
    if (!meter) return;
    var max = parseInt(card.dataset.max, 10);
    // 属性缺失（NaN）按无限制处理，比当成 0 容量安全
    var limited = !isNaN(max) && max >= 0;
    var ratio = limited ? Math.min((used * 100) / (max || 1), 100) : 0;
    var bar = meter.querySelector('i');
    if (bar) bar.style.width = ratio + '%';
    // 「快满了」的红条只有空间卡有（和模板一致）
    if (warnable) meter.classList.toggle('warn', limited && ratio > 85);
  }

  function uploadOne(file) {
    if(file.size > Number(dropzone.dataset.maxUploadBytes)) {
      FT.toast('「' + file.name + '」超过单文件大小上限', 'error');
      return;
    }
    // A server-side receipt is rediscovered by full-file SHA-256, even after reload.
    document.querySelectorAll('[data-pending-name]').forEach(function(el) {
      if(el.dataset.pendingName === file.name) el.remove();
    });
    var row = createRow(file);
    queue.appendChild(row.el);
    queue.hidden = false;
    row.el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    var current = { cancelled: false };
    var options = { expires_hours: Number(expiresSelect.value), is_public: publicCheckbox.checked };

    async function attempt() {
      if(row.isUploading()) return;
      if(reload.timer) {
        clearTimeout(reload.timer);
        reload.timer = null;
        reload.wanted = true;
      }
      current.cancelled = false;
      row.begin();
      active += 1;
      try {
        await FTUploads.run(file, options, current, row);
        row.done();
        uploadedSomething = true;
        FT.toast('「' + file.name + '」上传完成', 'success');
      } catch(error) {
        if(error.paused) row.cancelled();
        else {
          row.fail(error.message || '上传失败，已完成的分片已保留');
          FT.toast(error.message || '上传失败', 'error');
        }
      } finally {
        active -= 1;
        finish();
      }
    }

    row.dismiss.addEventListener('click', async function () {
      if(row.isUploading()) {
        FTUploads.pause(current);
        return;
      }
      if(current.id && !row.el.classList.contains('done')) {
        if(!await FT.confirm('放弃这次上传并删除已保存的分片？', {title:'放弃上传', okText:'放弃上传'})) return;
        try { await FTUploads.discard(current); }
        catch(error) { FT.toast(error.message, 'error'); return; }
      }
      row.el.remove();
      if(!queue.children.length) queue.hidden = true;
    });
    row.retry.addEventListener('click', attempt);
    attempt();
  }

  // Browsers require users to select local files again after a page is reopened.
  // Only names and receipts come from the server; local file bytes stay local.
  FT.request('/api/uploads').then(function(data) {
    (data.items || []).forEach(function(item) {
      if(Array.from(queue.children).some(function(el) { return el.dataset.fileName === item.name; })) return;
      var el = document.createElement('div');
      el.className = 'upload-item';
      el.dataset.pendingName = item.name;
      var name = document.createElement('div'); name.className = 'name'; name.textContent = item.name;
      var status = document.createElement('div'); status.className = 'tiny muted';
      status.textContent = '已保存 ' + FT.formatSize(item.offset) + ' / ' + FT.formatSize(item.size) + '，请选择原文件继续上传';
      var actions = document.createElement('div'); actions.className = 'upload-actions';
      var resume = document.createElement('button'); resume.type = 'button'; resume.className = 'btn btn-sm';
      resume.textContent = '继续'; resume.addEventListener('click', pick);
      var discard = document.createElement('button'); discard.type = 'button'; discard.className = 'btn btn-sm';
      discard.textContent = '放弃';
      discard.addEventListener('click', async function() {
        if(!await FT.confirm('放弃「' + item.name + '」并清理已上传分片？', {title:'放弃上传',okText:'放弃上传'})) return;
        try { await FTUploads.discard({id:item.id}); el.remove(); if(!queue.children.length) queue.hidden=true; }
        catch(error) { FT.toast(error.message,'error'); }
      });
      actions.append(resume,discard); el.append(name,status,actions); queue.appendChild(el); queue.hidden=false;
    });
  }).catch(function(error) { FT.toast('未能读取未完成上传：' + error.message, 'error'); });

  function finish() {
    if (active > 0) return; // 还有别的文件在传，等它一起收尾
    if (uploadedSomething) {
      // 让用户看清进度条完成，再刷新列表
      uploadedSomething = false;
      refresh(900);
      return;
    }
    if (reload.wanted) refresh(); // 兑现删除 / 修改文件时登记的刷新
  }

  /* ---------------------------------------------------------- 删除文件 */

  document.addEventListener('click', async function (event) {
    var button = event.target.closest('[data-delete-file]');
    if (!button) return;

    var name = button.getAttribute('data-name') || '该文件';
    // 卡片摘掉之后按钮就读不到了，先把大小拿在手里（配额卡要按它减）
    var size = parseInt(button.getAttribute('data-size'), 10) || 0;
    var confirmed = await FT.confirm('确定删除「' + name + '」吗？此操作不可恢复。', {
      title: '删除文件',
      okText: '删除',
    });
    if (!confirmed) return;

    var restore = FT.busy(button, true);
    try {
      await FT.request('/api/files/' + button.getAttribute('data-delete-file'), {
        method: 'DELETE',
      });
      var card = button.closest('.file-card');
      if (card) {
        card.style.transition = 'opacity .2s, transform .2s';
        card.style.opacity = '0';
        card.style.transform = 'scale(.96)';
        setTimeout(function () {
          card.remove();
          syncListAfterRemoval();
          syncQuotaAfterRemoval(size);
        }, 200);
      }
      FT.toast('文件已删除', 'success');
      // 走 refresh 而不是直接 reload：正在上传的文件不能被这一下掐断
      refresh(900);
    } catch (error) {
      FT.toast(error.message, 'error');
      restore();
    }
  });

  /* ---------------------------------------------------------- 修改文件 */

  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-edit-file]');
    if (!button) return;
    openEditModal(
      button.getAttribute('data-edit-file'),
      button.getAttribute('data-expires'),
      button.getAttribute('data-public') === 'true'
    );
  });

  function openEditModal(fileId, expiresIso, isPublic) {
    var backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';

    // expires_at 为空即长期有效；只有获得授权的用户能重新选这个值
    var isForever = !expiresIso;

    var options = EXPIRY_OPTIONS.filter(function (pair) {
      return pair[0] <= maxExpireDays * 24;
    });

    var selectHtml = options
      .map(function (pair) {
        var selected = pair[0] === 0 && isForever ? ' selected' : '';
        return '<option value="' + pair[0] + '"' + selected + '>' + pair[1] + '</option>';
      })
      .join('');

    var currentText = isForever
      ? '长期有效' + (canForever ? '' : '（由管理员设置）')
      : FT.formatTime(expiresIso);

    backdrop.innerHTML =
      '<div class="modal card" role="dialog" aria-modal="true" aria-label="修改文件设置">' +
      '  <div class="card-head"><h2>修改文件设置</h2>' +
      '    <button type="button" class="icon-btn" data-close aria-label="关闭">' +
      '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>' +
      '</button></div>' +
      '  <div class="card-body">' +
      '    <div class="field"><label for="edit-expires">保留时长（从现在重新计算）</label>' +
      '      <select id="edit-expires">' +
      selectHtml +
      '      </select>' +
      '      <span class="help">当前：' +
      currentText +
      '</span></div>' +
      '    <label class="checkbox"><input type="checkbox" id="edit-public" ' +
      (isPublic ? 'checked' : '') +
      ' />' +
      '      <span>公开分享<span class="tiny muted" style="display:block">取消后仅自己可见</span></span>' +
      '    </label>' +
      '  </div>' +
      '  <div class="card-foot row" style="justify-content: flex-end">' +
      '    <button type="button" class="btn" data-close>取消</button>' +
      '    <button type="button" class="btn btn-primary" data-save>保存</button>' +
      '  </div>' +
      '</div>';

    document.body.appendChild(backdrop);

    function close() {
      backdrop.remove();
      document.removeEventListener('keydown', onKey);
    }

    function onKey(event) {
      if (event.key === 'Escape') close();
    }

    document.addEventListener('keydown', onKey);

    backdrop.querySelectorAll('[data-close]').forEach(function (node) {
      node.addEventListener('click', close);
    });

    backdrop.addEventListener('click', function (event) {
      if (event.target === backdrop) close();
    });

    var saveButton = backdrop.querySelector('[data-save]');
    saveButton.addEventListener('click', async function () {
      var restore = FT.busy(saveButton, true);
      try {
        await FT.request('/api/files/' + fileId, {
          method: 'PATCH',
          body: {
            expires_hours: parseInt(backdrop.querySelector('#edit-expires').value, 10),
            is_public: backdrop.querySelector('#edit-public').checked,
          },
        });
        FT.toast('已保存', 'success');
        close();
        refresh(700);
      } catch (error) {
        FT.toast(error.message, 'error');
        restore();
      }
    });
  }
})();
