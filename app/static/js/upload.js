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

  function createRow(file) {
    var el = document.createElement('div');
    el.className = 'upload-item';
    el.innerHTML =
      '<div class="name"></div>' +
      '<div class="tiny muted status">等待中</div>' +
      '<div class="progress"><i></i></div>';

    el.querySelector('.name').textContent = file.name;
    var status = el.querySelector('.status');
    var bar = el.querySelector('.progress > i');

    return {
      el: el,
      setProgress: function (ratio) {
        bar.style.width = Math.round(ratio * 100) + '%';
        status.textContent = Math.round(ratio * 100) + '% · ' + FT.formatSize(file.size);
      },
      done: function () {
        el.classList.add('done');
        bar.style.width = '100%';
        status.textContent = '已完成';
      },
      fail: function (message) {
        el.classList.add('failed');
        status.textContent = message || '失败';
        status.style.color = 'var(--danger)';
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

    xhr.upload.addEventListener('progress', function (event) {
      if (event.lengthComputable) row.setProgress(event.loaded / event.total);
    });

    xhr.addEventListener('load', function () {
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
      row.fail('网络中断');
      FT.toast('「' + file.name + '」上传中断，请重试', 'error');
      finish();
    });

    xhr.addEventListener('abort', function () {
      row.fail('已取消');
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
    if (!window.confirm('确定删除「' + name + '」吗？此操作不可恢复。')) return;

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
