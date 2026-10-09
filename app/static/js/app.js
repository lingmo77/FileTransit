/* 文件中转站 —— 公共前端逻辑：主题、请求、提示、下拉菜单 */
(function () {
  'use strict';

  var THEME_KEY = 'ft-theme';

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';
  }

  var FT = (window.FT = {
    csrf: function () {
      var meta = document.querySelector('meta[name="csrf-token"]');
      return meta ? meta.content : '';
    },

    theme: {
      get: function () {
        return document.documentElement.dataset.theme || 'light';
      },
      set: function (value) {
        applyTheme(value);
        try {
          localStorage.setItem(THEME_KEY, value);
        } catch (e) {
          /* 隐私模式下 localStorage 不可用，忽略 */
        }
      },
      toggle: function () {
        this.set(this.get() === 'dark' ? 'light' : 'dark');
      },
    },

    /**
     * 统一请求封装：自动带 CSRF 头、自动解析 JSON、统一错误提示。
     */
    request: async function (url, options) {
      var opts = options || {};
      var method = (opts.method || 'GET').toUpperCase();
      var headers = Object.assign({}, opts.headers || {});

      if (method !== 'GET' && method !== 'HEAD') {
        headers['X-CSRF-Token'] = FT.csrf();
      }

      var fetchOptions = { method: method, headers: headers, credentials: 'same-origin' };

      if (opts.body !== undefined && opts.body !== null) {
        if (opts.form instanceof FormData) {
          fetchOptions.body = opts.form;
        } else {
          headers['Content-Type'] = 'application/json';
          fetchOptions.body = JSON.stringify(opts.body);
        }
      }

      var response = await fetch(url, fetchOptions);
      var contentType = response.headers.get('content-type') || '';
      var data = null;
      if (contentType.indexOf('application/json') !== -1) {
        data = await response.json().catch(function () {
          return null;
        });
      }

      if (!response.ok) {
        var message =
          (data && (data.error || data.detail || data.message)) ||
          '请求失败（HTTP ' + response.status + '）';
        if (typeof message !== 'string') message = JSON.stringify(message);
        var error = new Error(message);
        error.status = response.status;
        error.data = data;
        if (response.status === 401 && data && data.redirect === undefined) {
          // 会话过期：回登录页
          window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
        }
        throw error;
      }

      return data;
    },

    toast: function (message, type, timeout) {
      var root = document.getElementById('toast-root');
      if (!root) return;

      var el = document.createElement('div');
      el.className = 'toast ' + (type || 'info');
      el.setAttribute('role', type === 'error' ? 'alert' : 'status');
      el.textContent = String(message);
      root.appendChild(el);

      var life = timeout || (type === 'error' ? 5200 : 3200);
      setTimeout(function () {
        el.classList.add('leaving');
        setTimeout(function () {
          el.remove();
        }, 220);
      }, life);
    },

    /** 按钮忙碌态：返回一个恢复函数 */
    busy: function (button, busy) {
      if (!button) return function () {};
      if (busy === false) {
        button.removeAttribute('aria-busy');
        button.disabled = false;
        return function () {};
      }
      button.setAttribute('aria-busy', 'true');
      button.disabled = true;
      return function () {
        button.removeAttribute('aria-busy');
        button.disabled = false;
      };
    },

    copy: async function (text) {
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          ta.remove();
        }
        FT.toast('已复制到剪贴板', 'success');
        return true;
      } catch (e) {
        FT.toast('复制失败，请手动选择文本', 'error');
        return false;
      }
    },

    absoluteUrl: function (path) {
      return new URL(path, window.location.origin).href;
    },

    formatSize: function (bytes) {
      if (bytes === null || bytes === undefined) return '-';
      var units = ['B', 'KB', 'MB', 'GB', 'TB'];
      var size = Number(bytes);
      var i = 0;
      while (size >= 1024 && i < units.length - 1) {
        size /= 1024;
        i += 1;
      }
      return (i === 0 ? size : size.toFixed(size < 100 ? 2 : 1)) + ' ' + units[i];
    },

    formatTime: function (iso) {
      if (!iso) return '-';
      var date = new Date(iso);
      if (isNaN(date.getTime())) return iso;
      var pad = function (n) {
        return String(n).padStart(2, '0');
      };
      return (
        date.getFullYear() +
        '-' +
        pad(date.getMonth() + 1) +
        '-' +
        pad(date.getDate()) +
        ' ' +
        pad(date.getHours()) +
        ':' +
        pad(date.getMinutes())
      );
    },

    logout: async function () {
      try {
        var data = await FT.request('/api/auth/logout', { method: 'POST' });
        window.location.href = (data && data.redirect) || '/';
      } catch (e) {
        FT.toast(e.message, 'error');
      }
    },
  });

  /* ------------------------------------------------------------ 交互绑定 */

  function bindThemeToggle() {
    document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
      button.addEventListener('click', function () {
        FT.theme.toggle();
      });
    });
  }

  function bindDropdowns() {
    document.querySelectorAll('[data-dropdown]').forEach(function (wrap) {
      var trigger = wrap.querySelector('[data-dropdown-trigger]');
      if (!trigger) return;

      trigger.addEventListener('click', function (event) {
        event.stopPropagation();
        var isOpen = wrap.classList.contains('open');
        document.querySelectorAll('[data-dropdown].open').forEach(function (other) {
          other.classList.remove('open');
        });
        wrap.classList.toggle('open', !isOpen);
      });
    });

    document.addEventListener('click', function () {
      document.querySelectorAll('[data-dropdown].open').forEach(function (wrap) {
        wrap.classList.remove('open');
      });
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        document.querySelectorAll('[data-dropdown].open').forEach(function (wrap) {
          wrap.classList.remove('open');
        });
      }
    });
  }

  function bindLogout() {
    document.querySelectorAll('[data-logout]').forEach(function (button) {
      button.addEventListener('click', function () {
        FT.logout();
      });
    });
  }

  function bindCopy() {
    document.querySelectorAll('[data-copy]').forEach(function (button) {
      button.addEventListener('click', function () {
        var value = button.getAttribute('data-copy');
        FT.copy(value.indexOf('http') === 0 ? value : FT.absoluteUrl(value));
      });
    });
  }

  /** 跟随系统主题：用户没手动选过时实时同步 */
  function watchSystemTheme() {
    var media = window.matchMedia('(prefers-color-scheme: dark)');
    var handler = function (event) {
      var stored = null;
      try {
        stored = localStorage.getItem(THEME_KEY);
      } catch (e) {
        stored = null;
      }
      if (!stored && document.documentElement.dataset.themeDefault === 'system') {
        applyTheme(event.matches ? 'dark' : 'light');
      }
    };
    if (media.addEventListener) media.addEventListener('change', handler);
    else if (media.addListener) media.addListener(handler);
  }

  document.addEventListener('DOMContentLoaded', function () {
    bindThemeToggle();
    bindDropdowns();
    bindLogout();
    bindCopy();
    watchSystemTheme();
  });
})();
