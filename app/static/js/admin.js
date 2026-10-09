/* 后台管理交互：用户 / 文件 / 日志 / 设置 / 邮件 */
(function () {
  'use strict';

  var CLOSE_ICON =
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';

  /* ------------------------------------------------------------ 小工具 */

  function collectForm(form) {
    var values = {};
    Array.prototype.forEach.call(
      form.querySelectorAll('input, select, textarea'),
      function (el) {
        var name = el.name;
        if (!name || el.disabled) return;

        if (el.type === 'checkbox') {
          values[name] = el.checked;
          return;
        }
        if (el.type === 'radio') {
          if (el.checked) values[name] = el.value;
          return;
        }

        var raw = el.value;
        if (raw === '' && el.dataset.skipEmpty === 'true') return;

        if (el.dataset.transform === 'gb') {
          // 表单里用 GB 填写，接口需要字节；-1 表示不限制
          values[name] = raw.trim() === '-1' ? -1 : Math.round(parseFloat(raw) * 1024 * 1024 * 1024);
          return;
        }

        var type = el.dataset.type || 'str';
        if (type === 'int') {
          values[name] = raw === '' ? null : parseInt(raw, 10);
        } else if (type === 'float') {
          values[name] = raw === '' ? null : parseFloat(raw);
        } else if (type === 'bool') {
          values[name] = String(raw).toLowerCase() === 'true';
        } else {
          values[name] = raw;
        }
      }
    );
    return values;
  }

  function openModal(options) {
    var backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML =
      '<div class="modal card" role="dialog" aria-modal="true">' +
      '<div class="card-head"><h2>' +
      options.title +
      '</h2>' +
      '<button type="button" class="icon-btn" data-close aria-label="关闭">' +
      CLOSE_ICON +
      '</button></div>' +
      '<div class="card-body" data-modal-body></div>' +
      '<div class="card-foot row" style="justify-content: flex-end">' +
      '<button type="button" class="btn" data-close>取消</button>' +
      '<button type="button" class="btn btn-primary" data-ok>' +
      (options.okText || '确定') +
      '</button></div></div>';

    var body = backdrop.querySelector('[data-modal-body]');
    var okButton = backdrop.querySelector('[data-ok]');
    if (options.danger) okButton.classList.add('btn-danger');
    body.innerHTML = options.body || '';

    document.body.appendChild(backdrop);

    function close() {
      backdrop.remove();
      document.removeEventListener('keydown', onKey);
    }

    function onKey(event) {
      if (event.key === 'Escape') close();
    }

    document.addEventListener('keydown', onKey);
    Array.prototype.forEach.call(backdrop.querySelectorAll('[data-close]'), function (node) {
      node.addEventListener('click', close);
    });
    backdrop.addEventListener('click', function (event) {
      if (event.target === backdrop) close();
    });

    var first = body.querySelector('input, select, textarea');
    if (first) first.focus();

    return { root: backdrop, body: body, okButton: okButton, close: close };
  }

  function reload(delay) {
    setTimeout(function () {
      window.location.reload();
    }, delay === undefined ? 800 : delay);
  }

  function fill(id, value) {
    var el = document.getElementById(id);
    if (el) el.value = value === null || value === undefined ? '' : value;
  }

  function checked(id, value) {
    var el = document.getElementById(id);
    if (el) el.checked = !!value;
  }

  /* ------------------------------------------------------------ 动作表 */

  var ACTIONS = {
    maintenance: async function (button) {
      var restore = FT.busy(button, true);
      try {
        var data = await FT.request('/admin/api/maintenance/run', { method: 'POST' });
        var r = (data && data.result) || {};
        FT.toast(
          '维护完成：清理文件 ' +
            (r.expired_files || 0) +
            ' 个 · 会话 ' +
            (r.sessions || 0) +
            ' · 令牌 ' +
            (r.tokens || 0),
          'success'
        );
        reload(1200);
      } catch (error) {
        FT.toast(error.message, 'error');
        restore();
      }
    },

    'new-user': function () {
      var dialog = openModal({
        title: '新建用户',
        okText: '创建',
        body:
          '<div class="field"><label>用户名</label>' +
          '<input type="text" id="nu-username" maxlength="64" autocomplete="off" /></div>' +
          '<div class="field"><label>邮箱</label>' +
          '<input type="email" id="nu-email" maxlength="254" autocomplete="off" /></div>' +
          '<div class="field"><label>初始密码</label>' +
          '<input type="text" id="nu-password" autocomplete="off" />' +
          '<span class="help">用户首次登录后会被要求修改密码</span></div>' +
          '<label class="checkbox"><input type="checkbox" id="nu-admin" />' +
          '<span>设为管理员</span></label>' +
          '<label class="checkbox" style="margin-top:8px"><input type="checkbox" id="nu-permanent" />' +
          '<span>允许永久保存</span></label>' +
          '<label class="checkbox" style="margin-top:8px"><input type="checkbox" id="nu-verified" checked />' +
          '<span>邮箱标记为已验证</span></label>',
      });

      dialog.okButton.addEventListener('click', async function () {
        var username = dialog.body.querySelector('#nu-username').value.trim();
        var email = dialog.body.querySelector('#nu-email').value.trim();
        var password = dialog.body.querySelector('#nu-password').value;

        if (!username || !email || password.length < 8) {
          FT.toast('请填写完整信息，密码至少 8 位', 'error');
          return;
        }

        var restore = FT.busy(dialog.okButton, true);
        try {
          await FT.request('/admin/api/users', {
            method: 'POST',
            body: {
              username: username,
              email: email,
              password: password,
              is_admin: dialog.body.querySelector('#nu-admin').checked,
              can_permanent: dialog.body.querySelector('#nu-permanent').checked,
              email_verified: dialog.body.querySelector('#nu-verified').checked,
            },
          });
          FT.toast('用户已创建', 'success');
          dialog.close();
          reload();
        } catch (error) {
          FT.toast(error.message, 'error');
          restore();
        }
      });
    },

    'edit-user': function (button) {
      var id = button.getAttribute('data-id');
      var username = button.getAttribute('data-username') || '';

      var dialog = openModal({
        title: '编辑用户 · ' + username,
        okText: '保存',
        body:
          '<div class="field"><label>邮箱</label>' +
          '<input type="email" id="eu-email" autocomplete="off" value="" /></div>' +
          '<div class="field"><label>重置密码</label>' +
          '<input type="text" id="eu-password" autocomplete="off" placeholder="留空表示不修改" />' +
          '<span class="help">设置后该用户需重新登录并使用新密码</span></div>' +
          '<div class="form-grid">' +
          '<div class="field"><label>同时分享文件数上限</label>' +
          '<input type="number" id="eu-files" placeholder="留空 = 站点默认" /></div>' +
          '<div class="field"><label>总空间上限（GB）</label>' +
          '<input type="number" id="eu-gb" step="0.1" placeholder="留空 = 站点默认" /></div>' +
          '</div>' +
          '<p class="tiny muted" style="margin: 0">上限填 <code>-1</code> 表示不限制。</p>' +
          '<label class="checkbox" style="margin-top:12px"><input type="checkbox" id="eu-admin" />' +
          '<span>管理员</span></label>' +
          '<label class="checkbox" style="margin-top:8px"><input type="checkbox" id="eu-permanent" />' +
          '<span>允许永久保存</span></label>' +
          '<label class="checkbox" style="margin-top:8px"><input type="checkbox" id="eu-active" />' +
          '<span>账号启用</span></label>' +
          '<label class="checkbox" style="margin-top:8px"><input type="checkbox" id="eu-verified" />' +
          '<span>邮箱已验证</span></label>',
      });

      fill('eu-email', button.getAttribute('data-email') || '');
      checked('eu-admin', button.getAttribute('data-admin') === 'true');
      checked('eu-permanent', button.getAttribute('data-permanent') === 'true');
      checked('eu-active', button.getAttribute('data-active') === 'true');
      checked('eu-verified', button.getAttribute('data-verified') === 'true');
      fill('eu-files', button.getAttribute('data-quota-files'));
      var bytes = button.getAttribute('data-quota-bytes');
      fill('eu-gb', bytes ? (parseInt(bytes, 10) / 1024 / 1024 / 1024).toFixed(2) : '');

      dialog.okButton.addEventListener('click', async function () {
        var payload = {
          email: dialog.body.querySelector('#eu-email').value.trim(),
          is_admin: dialog.body.querySelector('#eu-admin').checked,
          is_active: dialog.body.querySelector('#eu-active').checked,
          email_verified: dialog.body.querySelector('#eu-verified').checked,
          can_permanent: dialog.body.querySelector('#eu-permanent').checked,
        };

        var password = dialog.body.querySelector('#eu-password').value;
        if (password) payload.password = password;

        var filesRaw = dialog.body.querySelector('#eu-files').value.trim();
        payload.quota_max_files = filesRaw === '' ? null : parseInt(filesRaw, 10);

        var gbRaw = dialog.body.querySelector('#eu-gb').value.trim();
        payload.quota_max_bytes =
          gbRaw === '' ? null : Math.round(parseFloat(gbRaw) * 1024 * 1024 * 1024);

        if (!payload.email) {
          FT.toast('邮箱不能为空', 'error');
          return;
        }

        var restore = FT.busy(dialog.okButton, true);
        try {
          await FT.request('/admin/api/users/' + id, { method: 'PATCH', body: payload });
          FT.toast('已保存', 'success');
          dialog.close();
          reload();
        } catch (error) {
          FT.toast(error.message, 'error');
          restore();
        }
      });
    },

    'delete-user': async function (button) {
      var username = button.getAttribute('data-username') || '';
      if (
        !window.confirm(
          '确定删除用户「' + username + '」？\n该用户的所有文件也会被一并删除，且不可恢复。'
        )
      )
        return;

      var restore = FT.busy(button, true);
      try {
        var data = await FT.request('/admin/api/users/' + button.getAttribute('data-id'), {
          method: 'DELETE',
        });
        FT.toast(data.message || '已删除', 'success');
        reload();
      } catch (error) {
        FT.toast(error.message, 'error');
        restore();
      }
    },

    'recount-user': async function (button) {
      var restore = FT.busy(button, true);
      try {
        var data = await FT.request(
          '/admin/api/users/' + button.getAttribute('data-id') + '/quota/recount',
          { method: 'POST' }
        );
        FT.toast(
          '已重算：' + data.used_files + ' 个文件 / ' + FT.formatSize(data.used_bytes),
          'success'
        );
        reload();
      } catch (error) {
        FT.toast(error.message, 'error');
        restore();
      }
    },

    'edit-file': function (button) {
      var id = button.getAttribute('data-id');
      var name = button.getAttribute('data-name') || '';

      var dialog = openModal({
        title: '编辑文件',
        okText: '保存',
        body:
          '<div class="field"><label>文件名</label>' +
          '<input type="text" id="ef-name" maxlength="255" value="" /></div>' +
          '<div class="field"><label>剩余保留时长</label>' +
          '<select id="ef-hours">' +
          '<option value="">不修改</option>' +
          '<option value="1">1 小时</option>' +
          '<option value="6">6 小时</option>' +
          '<option value="24">1 天</option>' +
          '<option value="72">3 天</option>' +
          '<option value="168">7 天</option>' +
          '<option value="336">14 天</option>' +
          '<option value="720">30 天</option>' +
          '<option value="0">长期有效</option>' +
          '</select>' +
          '<span class="help">选择后从现在开始重新计算；长期有效的文件不会被自动清理</span></div>' +
          '<label class="checkbox" style="margin-top:8px"><input type="checkbox" id="ef-public" />' +
          '<span>公开分享</span></label>',
      });

      fill('ef-name', name);
      checked('ef-public', button.getAttribute('data-public') === 'true');

      dialog.okButton.addEventListener('click', async function () {
        var payload = {
          original_name: dialog.body.querySelector('#ef-name').value.trim(),
          is_public: dialog.body.querySelector('#ef-public').checked,
        };
        // 空串 = 不修改；"0" = 长期有效，必须原样送出
        var hours = dialog.body.querySelector('#ef-hours').value;
        if (hours !== '') payload.expires_hours = parseInt(hours, 10);

        var restore = FT.busy(dialog.okButton, true);
        try {
          await FT.request('/admin/api/files/' + id, { method: 'PATCH', body: payload });
          FT.toast('已保存', 'success');
          dialog.close();
          reload();
        } catch (error) {
          FT.toast(error.message, 'error');
          restore();
        }
      });
    },

    'delete-file': async function (button) {
      var name = button.getAttribute('data-name') || '';
      if (!window.confirm('确定删除文件「' + name + '」？磁盘上的文件会被立即清除。')) return;

      var restore = FT.busy(button, true);
      try {
        await FT.request('/admin/api/files/' + button.getAttribute('data-id'), {
          method: 'DELETE',
        });
        FT.toast('文件已删除', 'success');
        var row = button.closest('tr');
        if (row) row.remove();
        reload(1200);
      } catch (error) {
        FT.toast(error.message, 'error');
        restore();
      }
    },

    'purge-logs': function (button) {
      var form = document.getElementById('log-filter-form');
      var before = (document.getElementById('purge-before') || {}).value || '';
      var after = (document.getElementById('purge-after') || {}).value || '';
      var scopeAction = (document.getElementById('purge-action') || {}).value || '';

      if (!before && !after && !scopeAction) {
        FT.toast('请至少指定时间范围或日志类型，避免误删全部日志', 'error');
        return;
      }

      var description =
        (before ? '在 ' + before + ' 之前' : '') +
        (after ? (before ? '、' : '') + '在 ' + after + ' 之后' : '') +
        (scopeAction ? '、类型为「' + scopeAction + '」' : '');
      if (!window.confirm('确定清空' + description + '的日志？此操作不可恢复。')) return;

      var body = {
        before: before,
        after: after,
        action: scopeAction,
        vacuum: !!(document.getElementById('purge-vacuum') || {}).checked,
      };

      FT.busy(button, true);
      FT.request('/admin/api/logs', { method: 'DELETE', body: body })
        .then(function (data) {
          FT.toast(data.message || '已清理', 'success');
          // 稍等一下再刷新，否则 submit() 会把刚弹出的提示一起刷掉
          setTimeout(function () {
            if (form) form.submit();
            else reload();
          }, 1200);
        })
        .catch(function (error) {
          FT.toast(error.message, 'error');
          FT.busy(button, false);
        });
    },

    'log-detail': function (button) {
      var raw = button.getAttribute('data-detail') || '';
      var pretty = raw;
      try {
        pretty = JSON.stringify(JSON.parse(raw), null, 2);
      } catch (error) {
        pretty = raw;
      }

      var lines =
        '<dl class="kv" style="margin-top:0">' +
        '<dt>时间</dt><dd>' +
        (button.getAttribute('data-time') || '-') +
        '</dd>' +
        '<dt>用户</dt><dd>' +
        (button.getAttribute('data-user') || '-') +
        '</dd>' +
        '<dt>动作</dt><dd>' +
        (button.getAttribute('data-action-label') || '-') +
        '</dd>' +
        '<dt>IP</dt><dd>' +
        (button.getAttribute('data-ip') || '-') +
        '</dd>' +
        '<dt>客户端</dt><dd>' +
        (button.getAttribute('data-browser') || '-') +
        '</dd>' +
        '<dt>系统</dt><dd>' +
        (button.getAttribute('data-os') || '-') +
        '</dd>' +
        '<dt>UA</dt><dd class="tiny">' +
        (button.getAttribute('data-ua') || '-') +
        '</dd>' +
        '</dl>';

      var dialog = openModal({
        title: '日志详情',
        okText: '关闭',
        body:
          lines +
          (pretty
            ? '<pre class="code-block" style="margin-top:14px">' +
              pretty.replace(/[<>&]/g, function (c) {
                return { '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c];
              }) +
              '</pre>'
            : ''),
      });

      dialog.okButton.addEventListener('click', function () {
        dialog.close();
      });
    },
  };

  document.addEventListener('click', function (event) {
    var element = event.target.closest('[data-action]');
    if (!element) return;
    var handler = ACTIONS[element.getAttribute('data-action')];
    if (!handler) return;
    event.preventDefault();
    handler(element);
  });

  /* ------------------------------------------------------------ 表单提交 */

  var FORMS = {
    settings: async function (form) {
      var payload = collectForm(form);
      var data = await FT.request('/admin/api/settings', { method: 'POST', body: payload });
      FT.toast((data && data.message) || '设置已保存', 'success');
    },

    'mail-test': async function (form) {
      var data = await FT.request('/admin/api/email/test', {
        method: 'POST',
        body: collectForm(form),
      });
      FT.toast((data && data.message) || '测试邮件已发送', 'success');
    },

    'create-user': async function (form) {
      var data = await FT.request('/admin/api/users', {
        method: 'POST',
        body: collectForm(form),
      });
      FT.toast('用户已创建', 'success');
      if (data) reload();
    },
  };

  document.addEventListener('submit', async function (event) {
    var form = event.target.closest('form[data-form]');
    if (!form) return;
    var handler = FORMS[form.getAttribute('data-form')];
    if (!handler) return;

    event.preventDefault();
    var button = form.querySelector('[type="submit"]');
    var restore = FT.busy(button, true);
    try {
      await handler(form);
    } catch (error) {
      FT.toast(error.message, 'error');
      restore();
    }
  });
})();
