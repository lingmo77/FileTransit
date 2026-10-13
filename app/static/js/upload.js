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
    'stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';

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
    el.innerHTML =
      '<div class="name"></div>' +
      '<div class="tiny muted status">等待中</div>' +
      '<button type="button" class="icon-btn upload-dismiss" title="取消上传" ' +
      'aria-label="取消上传">' +
      DISMISS_ICON +
      '</button>' +
      '<div class="progress"><i></i></div>';

    el.querySelector('.name').textContent = file.name;
    var status = el.querySelector('.status');
    var bar = el.querySelector('.progress > i');
    var dismiss = el.querySelector('.upload-dismiss');

    function paint(text) {
      status.textContent = text;
    }

    /** 收尾后按钮从「取消上传」变成「从列表移除」 */
    function settle(className, text) {
      el.classList.add(className);
      paint(text);
      dismiss.title = '从列表移除';
      dismiss.setAttribute('aria-label', '从列表移除');
    }

    return {
      el: el,
      dismiss: dismiss,

      /** 上传中：百分比 + 实时速度 + 预计剩余时间 */
      progress: function (ratio, speed, loaded, total) {
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

      /** 超过 2 秒没有收到进度事件：把速度归零，别让读数停在一个骗人的值上 */
      stalled: function (percent) {
        paint(percent + ' · 已停顿…');
      },

      done: function () {
        bar.style.width = '100%';
        settle('done', '已完成');
      },
      fail: function (message) {
        settle('failed', message || '失败');
        status.style.color = 'var(--danger)';
      },
      cancelled: function () {
        settle('cancelled', '已取消');
      },
    };
  }

  function uploadAll(fileList) {
    Array.prototype.forEach.call(fileList, uploadOne);
  }

  function uploadOne(file) {
    var row = createRow(file);
    queue.appendChild(row.el);
    queue.hidden = false;
    active += 1;
    row.el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });

    var form = new FormData();
    form.append('file', file);
    form.append('expires_hours', expiresSelect.value);
    form.append('is_public', publicCheckbox.checked ? 'true' : 'false');

    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/files', true);
    xhr.setRequestHeader('X-CSRF-Token', FT.csrf());

    // 速度表。浏览器的进度事件既密集又不均匀，直接拿两次事件的差算瞬时值会
    // 跳得没法看，所以每 500ms 采一次样，再做指数平滑。
    var sample = { at: 0, loaded: 0 };
    var meter = { at: 0, speed: 0 };
    var lastLoaded = 0;
    var lastTotal = file.size || 0;
    var settled = false;
    var ticker = null;

    function stopTicker() {
      if (ticker) {
        clearInterval(ticker);
        ticker = null;
      }
    }

    xhr.upload.addEventListener('progress', function (event) {
      if (!event.lengthComputable) return;

      var now = Date.now();
      lastLoaded = event.loaded;
      lastTotal = event.total;
      meter.at = now;

      if (!sample.at) {
        sample.at = now;
        sample.loaded = event.loaded;
      }
      var elapsed = now - sample.at;
      if (elapsed >= 500) {
        var instant = (event.loaded - sample.loaded) / (elapsed / 1000);
        meter.speed = meter.speed > 0 ? meter.speed * 0.7 + instant * 0.3 : instant;
        sample.at = now;
        sample.loaded = event.loaded;
      }

      row.progress(event.loaded / event.total, meter.speed, event.loaded, event.total);
    });

    // 卡住的时候不再有进度事件，速度读数会永远停在最后一个采样值上。
    // 这里兜一个定时器：2 秒没动静就把速度归零，并明说"已停顿"。
    ticker = setInterval(function () {
      if (settled || !meter.at) return;
      if (Date.now() - meter.at < 2000) return;
      if (lastTotal > 0 && lastLoaded >= lastTotal) return; // 已在服务端处理阶段
      row.stalled(lastTotal ? lastLoaded / lastTotal : 0);
    }, 1000);

    // 上传中点击＝取消；已结束后点击＝把这一行从列表里去掉
    row.dismiss.addEventListener('click', function () {
      if (settled) {
        row.el.remove();
        // 全清空后别留一个空盒子占着地方
        if (!queue.children.length) queue.hidden = true;
        return;
      }
      xhr.abort(); // 收尾统一走 abort 事件
    });

    xhr.addEventListener('load', function () {
      settled = true;
      stopTicker();

      var data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch (e) {
        data = null;
      }

      if (xhr.status >= 200 && xhr.status < 300) {
        row.done();
        uploadedSomething = true;
        FT.toast('「' + file.name + '」上传完成', 'success');
      } else {
        var message =
          (data && (data.error || data.detail || data.message)) ||
          '上传失败（HTTP ' + xhr.status + '）';
        if (typeof message !== 'string') message = '上传失败';
        row.fail(message);
        FT.toast('「' + file.name + '」' + message, 'error');
      }
      finish();
    });

    xhr.addEventListener('error', function () {
      settled = true;
      stopTicker();
      row.fail('网络中断');
      FT.toast('「' + file.name + '」上传中断，请重试', 'error');
      finish();
    });

    xhr.addEventListener('abort', function () {
      settled = true;
      stopTicker();
      row.cancelled();
      FT.toast('已取消上传「' + file.name + '」', 'info');
      finish();
    });

    xhr.send(form);
  }

  function finish() {
    active -= 1;
    if (active > 0) return;
    if (!uploadedSomething) return;
    // 让用户看清进度条完成，再刷新列表
    uploadedSomething = false;
    setTimeout(function () {
      window.location.reload();
    }, 900);
  }

  /* ---------------------------------------------------------- 删除文件 */

  document.addEventListener('click', async function (event) {
    var button = event.target.closest('[data-delete-file]');
    if (!button) return;

    var name = button.getAttribute('data-name') || '该文件';
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
        }, 200);
      }
      FT.toast('文件已删除', 'success');
      setTimeout(function () {
        window.location.reload();
      }, 900);
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
        setTimeout(function () {
          window.location.reload();
        }, 700);
      } catch (error) {
        FT.toast(error.message, 'error');
        restore();
      }
    });
  }
})();
