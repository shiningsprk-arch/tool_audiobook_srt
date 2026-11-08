/**
 * audiobook_srt —— 工具前端逻辑（原生 JS，无构建）。
 *
 * 通道与其它外置工具一致：宿主 toolbox-bridge.js 提供 toolId / theme / locale / fetch，
 * 本地裸开 index.html 时退回同源 fetch。转写、装依赖、下模型都是后台任务，按秒轮询 tasks。
 */
(function () {
  'use strict';

  var bridge = window.MyBooksToolBridge || null;
  var i18n = window.MyBooksToolI18n.create();
  var TOOL_ID = (bridge && bridge.toolId) || 'audiobook_srt';
  var POLL_MS = 1500;
  var BOOK_REFRESH_MS = 3000;

  function el(id) {
    return document.getElementById(id);
  }

  var dom = {
    envAlert: el('env-alert'),
    refreshEnv: el('refresh-env'),
    envPython: el('env-python'),
    envPlatform: el('env-platform'),
    envDeps: el('env-deps'),
    envDisk: el('env-disk'),
    pipInput: el('pip-input'),
    hfInput: el('hf-input'),
    installBtn: el('install-btn'),
    installCancel: el('install-cancel'),
    saveEndpoint: el('save-endpoint'),
    installTask: el('install-task'),
    installBar: el('install-bar'),
    installStatus: el('install-status'),
    installPct: el('install-pct'),
    installLog: el('install-log'),
    modelsBody: el('models-body'),
    downloadTask: el('download-task'),
    downloadBar: el('download-bar'),
    downloadStatus: el('download-status'),
    downloadPct: el('download-pct'),
    downloadCancel: el('download-cancel'),
    purgeBtn: el('purge-btn'),
    purgeSummary: el('purge-summary'),
    bookSearch: el('book-search'),
    onlyMissing: el('only-missing'),
    refreshBooks: el('refresh-books'),
    audioFolder: el('audio-folder'),
    booksBody: el('books-body'),
    bookCard: el('book-card'),
    bookTitle: el('book-title'),
    bookMeta: el('book-meta'),
    openPlayer: el('open-player'),
    modeSelect: el('mode-select'),
    selectAll: el('select-all'),
    selectCount: el('select-count'),
    fileBody: el('file-body'),
    modelSelect: el('model-select'),
    languageSelect: el('language-select'),
    computeSelect: el('compute-select'),
    threadsInput: el('threads-input'),
    beamInput: el('beam-input'),
    promptInput: el('prompt-input'),
    chunkCheck: el('chunk-check'),
    saveConfig: el('save-config'),
    startBtn: el('start-btn'),
    cancelBtn: el('cancel-btn'),
    jobTask: el('job-task'),
    jobBar: el('job-bar'),
    jobStatus: el('job-status'),
    jobPct: el('job-pct'),
    jobMessage: el('job-message'),
    jobError: el('job-error'),
    jobLog: el('job-log'),
    jobFilesBody: el('job-files-body'),
  };

  var state = {
    overview: null,
    books: [],
    book: null,
    bookId: null,
    tasks: {},
    mode: 'missing',
    selected: {},
    pollTimer: null,
    pollBusy: false,
    purging: false,
    lastBookRefresh: 0,
    booksLoaded: false,
  };

  // ---------------------------------------------------------------- 基础

  function applyTheme(theme) {
    document.body.setAttribute('data-theme', theme);
  }

  applyTheme((bridge && bridge.theme) || 'light');
  if (bridge && bridge.onThemeChange) {
    bridge.onThemeChange(applyTheme);
  }

  function esc(value) {
    return String(value === undefined || value === null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function formatBytes(bytes) {
    var value = Number(bytes || 0);
    if (!value) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return (value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1)) + ' ' + units[index];
  }

  function formatTime(ts) {
    if (!ts) return '-';
    try {
      return new Date(Number(ts) * 1000).toLocaleString();
    } catch (err) {
      return '-';
    }
  }

  function notify(message, type) {
    if (bridge && typeof bridge.notify === 'function') {
      bridge.notify(message, type || 'info');
    } else {
      window.alert(message);
    }
  }

  function rawRequest(path, options) {
    var url = '/api/toolbox/tool/' + TOOL_ID + '/' + path;
    var opts = options || {};
    if (!opts.credentials) opts.credentials = 'include';
    return fetch(url, opts).then(function (resp) {
      var contentType = resp.headers.get('content-type') || '';
      return contentType.indexOf('application/json') !== -1 ? resp.json() : resp.text();
    }).then(function (data) {
      if (typeof data === 'string') {
        try {
          return JSON.parse(data);
        } catch (err) {
          return { err: 'http', msg: data };
        }
      }
      return data;
    });
  }

  function request(path, options) {
    if (bridge && typeof bridge.fetch === 'function') {
      return bridge.fetch(path, options);
    }
    return rawRequest(path, options);
  }

  function getJson(path, params) {
    var query = [];
    Object.keys(params || {}).forEach(function (key) {
      query.push(encodeURIComponent(key) + '=' + encodeURIComponent(params[key]));
    });
    return request(query.length ? path + '?' + query.join('&') : path, { method: 'GET' });
  }

  function postJson(path, data) {
    return request(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data || {}),
    });
  }

  function errText(resp) {
    if (!resp) return i18n.t('error.unknown');
    return resp.msg || resp.err || i18n.t('error.unknown');
  }

  function statusKey(status) {
    var map = {
      pending: 'statusPending',
      running: 'statusRunning',
      done: 'statusDone',
      skipped: 'statusSkipped',
      failed: 'statusFailed',
      completed: 'statusCompleted',
      cancelled: 'statusCancelled',
      interrupted: 'statusInterrupted',
    };
    return map[status] || 'statusPending';
  }

  function statusClass(status) {
    if (status === 'done' || status === 'completed') return 'badge badge--ok';
    if (status === 'running') return 'badge badge--run';
    if (status === 'failed') return 'badge badge--err';
    if (status === 'interrupted' || status === 'skipped') return 'badge badge--warn';
    return 'badge';
  }

  function taskRunning(kind) {
    var task = state.tasks[kind];
    return !!(task && task.status === 'running');
  }

  function anyRunning() {
    return taskRunning('install') || taskRunning('download') || taskRunning('transcribe');
  }

  // ---------------------------------------------------------------- 环境/模型

  function refreshOverview() {
    return getJson('overview').then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        notify(errText(resp), 'error');
        return null;
      }
      state.overview = resp.data;
      state.tasks = resp.data.tasks || {};
      if (!dom.pipInput.value && state.overview.config.pip_index) {
        dom.pipInput.value = state.overview.config.pip_index;
      }
      if (!dom.hfInput.value) {
        dom.hfInput.value = state.overview.config.hf_endpoint || state.overview.env.default_endpoint || '';
      }
      renderEnv();
      renderModels();
      if (state.book) {
        populateModelSelect();
        renderBook();
        updateStartButton();
      }
      return state.overview;
    });
  }

  function renderEnv() {
    var env = state.overview && state.overview.env;
    if (!env) return;
    dom.envPython.textContent = env.python || '-';
    dom.envPlatform.textContent = env.platform || '-';
    if (env.ready) {
      var pkgs = env.packages || {};
      dom.envDeps.textContent = i18n.t('depsReady', {
        fw: pkgs['faster-whisper'] || '?',
        ct: pkgs.ctranslate2 || '?',
      });
    } else {
      dom.envDeps.textContent = i18n.t('depsMissing');
    }
    var disk = (state.overview.disk && state.overview.disk.data) || {};
    dom.envDisk.textContent = disk.free ? formatBytes(disk.free) : '-';
    if (document.activeElement !== dom.pipInput) {
      dom.pipInput.value = state.overview.config.pip_index || '';
    }
    if (document.activeElement !== dom.hfInput) {
      dom.hfInput.value = state.overview.config.hf_endpoint || env.default_endpoint || '';
    }

    var install = state.tasks.install;
    var transcribe = state.tasks.transcribe;
    dom.installBtn.disabled = !!(install && install.status === 'running') ||
      !!(transcribe && transcribe.status === 'running');
    dom.installCancel.hidden = !(install && install.status === 'running');
    if (install) {
      dom.installTask.hidden = install.status === 'completed';
      dom.installBar.style.width = (install.progress || 0) + '%';
      dom.installStatus.textContent = install.status === 'failed'
        ? (install.error || install.message || statusLabel(install.status))
        : (install.message || statusLabel(install.status));
      dom.installPct.textContent = Math.round(install.progress || 0) + '%';
      dom.installLog.hidden = !(install.logs && install.logs.length);
      dom.installLog.textContent = (install.logs || []).slice(-40).join('\n');
    } else {
      dom.installTask.hidden = true;
    }

    var download = state.tasks.download;
    dom.downloadCancel.hidden = !(download && download.status === 'running');
    if (download) {
      dom.downloadTask.hidden = download.status === 'completed';
      dom.downloadBar.style.width = (download.progress || 0) + '%';
      dom.downloadStatus.textContent = download.status === 'failed'
        ? (download.error || download.message || statusLabel(download.status))
        : (download.message || statusLabel(download.status));
      dom.downloadPct.textContent = Math.round(download.progress || 0) + '%';
    } else {
      dom.downloadTask.hidden = true;
    }

    var models = state.overview.models || [];
    var modelsSize = models.reduce(function (total, model) {
      return total + (model.installed ? Number(model.size || 0) : 0);
    }, 0);
    var depsSize = Number((env && env.size) || 0);
    dom.purgeSummary.textContent = i18n.t('purgeSizes', {
      deps: formatBytes(depsSize),
      models: formatBytes(modelsSize),
    });
    var purgeBusy = taskRunning('install') || taskRunning('download') || taskRunning('transcribe') || state.purging;
    dom.purgeBtn.disabled = purgeBusy || (!depsSize && !modelsSize);
    dom.purgeBtn.textContent = i18n.t(state.purging ? 'purgeBusy' : 'purgeButton');

    var installed = (state.overview.models || []).filter(function (model) { return model.installed; });
    if (!env.ready) {
      dom.envAlert.hidden = false;
      dom.envAlert.className = 'mb-alert mb-alert--warning';
      dom.envAlert.textContent = i18n.t('alertDepsMissing');
    } else if (!installed.length) {
      dom.envAlert.hidden = false;
      dom.envAlert.className = 'mb-alert mb-alert--warning';
      dom.envAlert.textContent = i18n.t('alertNoModel');
    } else {
      dom.envAlert.hidden = true;
    }
  }

  function statusLabel(status) {
    return i18n.t(statusKey(status));
  }

  function renderModels() {
    var models = (state.overview && state.overview.models) || [];
    if (!models.length) {
      dom.modelsBody.innerHTML = '<tr><td colspan="4" class="empty">' + esc(i18n.t('loading')) + '</td></tr>';
      return;
    }
    var download = state.tasks.download;
    var transcribe = state.tasks.transcribe;
    var rows = models.map(function (model) {
      var status = '<span class="badge">' + esc(i18n.t('modelNotInstalled')) + '</span>';
      if (model.installed) {
        status = '<span class="badge badge--ok">' + esc(i18n.t('modelInstalled')) + '</span>';
      } else if (download && download.status === 'running' && download.meta && download.meta.model === model.name) {
        status = '<span class="badge badge--run">' + esc(i18n.t('modelDownloading')) + ' ' +
          Math.round(download.progress || 0) + '%</span>';
      }
      var action;
      if (model.installed) {
        var busy = !!(transcribe && transcribe.status === 'running' && transcribe.meta &&
          transcribe.meta.model === model.name);
        action = '<button class="mb-btn mb-btn--secondary row-btn" data-action="delete" data-name="' +
          esc(model.name) + '"' + (busy ? ' disabled' : '') + '>' + esc(i18n.t('delete')) + '</button>';
      } else {
        var blocked = !!(download && download.status === 'running');
        action = '<button class="mb-btn row-btn" data-action="download" data-name="' + esc(model.name) + '"' +
          (blocked ? ' disabled' : '') + '>' + esc(i18n.t('download')) + '</button>';
      }
      var noteKey = 'modelNote.' + model.name;
      var noteText = i18n.t(noteKey);
      if (noteText === noteKey) noteText = model.note || '';
      return '<tr>' +
        '<td><b>' + esc(model.label) + '</b><div class="hint">' + esc(noteText) + '</div></td>' +
        '<td>' + formatBytes(model.installed ? model.size : model.expected_size) + '</td>' +
        '<td>' + status + '</td>' +
        '<td>' + action + '</td>' +
        '</tr>';
    });
    dom.modelsBody.innerHTML = rows.join('');
  }

  // ---------------------------------------------------------------- 书单

  function refreshBooks() {
    return getJson('books').then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        notify(errText(resp), 'error');
        return;
      }
      state.books = resp.data.books || [];
      state.booksLoaded = true;
      dom.audioFolder.textContent = i18n.t('audioFolder', { path: resp.data.audio_folder || '-' });
      renderBooks();
      if (state.bookId) {
        var found = state.books.filter(function (book) { return book.book_id === state.bookId; })[0];
        if (found) {
          dom.bookTitle.textContent = found.title || i18n.t('unknownTitle');
        }
      }
    });
  }

  function bookMatches(book) {
    if (dom.onlyMissing.checked && !book.missing_count) return false;
    var keyword = (dom.bookSearch.value || '').trim().toLowerCase();
    if (!keyword) return true;
    var haystack = [book.title].concat(book.authors || []).join(' ').toLowerCase();
    return haystack.indexOf(keyword) !== -1;
  }

  function jobSummary(job) {
    if (!job) return '-';
    var parts = [statusLabel(job.status)];
    if (job.total) parts.push(job.done + '/' + job.total);
    if (job.failed) parts.push(i18n.t('jobFailedCount', { n: job.failed }));
    return parts.join(' · ');
  }

  function renderBooks() {
    var books = state.books.filter(bookMatches);
    if (!books.length) {
      dom.booksBody.innerHTML = '<tr><td colspan="6" class="empty">' + esc(i18n.t('noBooks')) + '</td></tr>';
      return;
    }
    dom.booksBody.innerHTML = books.map(function (book) {
      return '<tr>' +
        '<td class="muted">' + book.book_id + '</td>' +
        '<td><b>' + esc(book.title || i18n.t('unknownTitle')) + '</b><div class="hint">' + esc((book.authors || []).join(' / ')) + '</div></td>' +
        '<td>' + book.audio_count + '</td>' +
        '<td>' + book.covered_count + ' / ' + book.audio_count +
        (book.missing_count ? ' <span class="badge badge--warn">' + esc(i18n.t('missingBadge', { n: book.missing_count })) + '</span>' : '') +
        '</td>' +
        '<td>' + esc(jobSummary(book.job)) + '</td>' +
        '<td><button class="mb-btn mb-btn--secondary row-btn" data-book-id="' + book.book_id + '">' +
        esc(i18n.t('selectBook')) + '</button></td>' +
        '</tr>';
    }).join('');
  }

  function openBook(bookId, resetOptions) {
    return getJson('book', { book_id: bookId }).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        notify(errText(resp), 'error');
        return;
      }
      state.book = resp.data.book;
      state.bookId = resp.data.book.book_id;
      if (resetOptions) {
        state.mode = 'missing';
        state.selected = {};
        applyConfigToInputs();
      }
      renderBook();
      dom.bookCard.hidden = false;
      if (resetOptions) {
        dom.bookCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  }

  function applyConfigToInputs() {
    var config = (state.overview && state.overview.config) || {};
    populateModelSelect();
    if (config.model) dom.modelSelect.value = config.model;
    dom.languageSelect.value = config.language || 'zh';
    dom.computeSelect.value = config.compute_type || 'int8';
    dom.threadsInput.value = config.cpu_threads || 2;
    dom.beamInput.value = config.beam_size || 5;
    dom.promptInput.value = config.initial_prompt || '';
    dom.chunkCheck.checked = config.chunk_enabled !== false;
  }

  function populateModelSelect() {
    var models = (state.overview && state.overview.models) || [];
    if (!models.length) return;
    var current = dom.modelSelect.value;
    dom.modelSelect.innerHTML = models.map(function (model) {
      return '<option value="' + esc(model.name) + '">' + esc(model.label) +
        (model.installed ? '' : ' (' + esc(i18n.t('modelNotInstalled')) + ')') + '</option>';
    }).join('');
    if (current) dom.modelSelect.value = current;
  }

  function currentFiles() {
    return (state.book && state.book.files) || [];
  }

  function selectedFiles() {
    var files = currentFiles();
    if (state.mode === 'all') return files.map(function (file) { return file.filename; });
    if (state.mode === 'missing') {
      return files.filter(function (file) { return !file.covered; }).map(function (file) { return file.filename; });
    }
    return files.filter(function (file) { return state.selected[file.filename]; }).map(function (file) { return file.filename; });
  }

  function modelInstalled(name) {
    var models = (state.overview && state.overview.models) || [];
    var found = models.filter(function (model) { return model.name === name; })[0];
    return !!(found && found.installed);
  }

  function renderBook() {
    var book = state.book;
    if (!book) return;
    dom.bookTitle.textContent = book.title || i18n.t('unknownTitle');
    dom.bookMeta.textContent = i18n.t('bookMeta', {
      id: book.book_id,
      authors: (book.authors || []).join(' / ') || '-',
      audio: book.audio_count,
      covered: book.covered_count,
      size: formatBytes(book.total_size),
    });
    dom.openPlayer.href = '/audio/' + book.book_id;
    dom.modeSelect.value = state.mode;
    dom.selectAll.hidden = state.mode !== 'selected';

    var files = currentFiles();
    dom.fileBody.innerHTML = files.map(function (file) {
      var checked = state.selected[file.filename] ? ' checked' : '';
      var disabled = state.mode === 'selected' ? '' : ' disabled';
      var sub = file.subtitle || i18n.t('noSubtitle');
      var badge;
      if (file.job_status) {
        badge = '<span class="' + statusClass(file.job_status) + '">' + esc(statusLabel(file.job_status)) + '</span>';
      } else if (file.covered) {
        badge = '<span class="badge badge--ok">' + esc(i18n.t('hasSubtitle')) + '</span>';
      } else {
        badge = '<span class="badge badge--warn">' + esc(i18n.t('missingSubtitle')) + '</span>';
      }
      return '<tr>' +
        '<td><input class="file-check" type="checkbox" data-file="' + esc(file.filename) + '"' +
        checked + disabled + ' /></td>' +
        '<td>' + esc(file.filename) + '</td>' +
        '<td>' + formatBytes(file.size) + '</td>' +
        '<td>' + esc(sub) + '</td>' +
        '<td>' + badge + '</td>' +
        '</tr>';
    }).join('');
    if (!files.length) {
      dom.fileBody.innerHTML = '<tr><td colspan="5" class="empty">' + esc(i18n.t('noFiles')) + '</td></tr>';
    }

    var chosen = selectedFiles();
    dom.selectCount.textContent = i18n.t('selectedCount', { n: chosen.length });
    renderJob();
    updateStartButton();
  }

  function renderJob() {
    var op = state.tasks.transcribe;
    var job = state.book && state.book.job;
    var totals = { total: 0, done: 0, skipped: 0, failed: 0, pending: 0 };
    (currentFiles() || []).forEach(function (file) {
      var status = file.job_status || 'pending';
      totals.total += 1;
      if (totals[status] !== undefined) totals[status] += 1;
    });

    if (!op && !job) {
      dom.jobTask.hidden = true;
      dom.cancelBtn.hidden = true;
      return;
    }
    dom.jobTask.hidden = false;
    if (op) {
      dom.jobBar.style.width = (op.progress || 0) + '%';
      dom.jobStatus.textContent = op.message || statusLabel(op.status);
      dom.jobPct.textContent = Math.round(op.progress || 0) + '%';
      dom.jobError.hidden = !op.error;
      dom.jobError.textContent = op.error || '';
      dom.jobLog.hidden = !(op.logs && op.logs.length);
      dom.jobLog.textContent = (op.logs || []).slice(-50).join('\n');
      dom.cancelBtn.hidden = op.status !== 'running';
      dom.jobMessage.textContent = i18n.t('jobCounts', {
        done: totals.done, skipped: totals.skipped, failed: totals.failed, total: totals.total,
      });
    } else {
      var progress = totals.total
        ? ((totals.done + totals.skipped + totals.failed) * 100 / totals.total) : 0;
      dom.jobBar.style.width = progress + '%';
      dom.jobStatus.textContent = statusLabel(job.status);
      dom.jobPct.textContent = Math.round(progress) + '%';
      dom.jobError.hidden = !job.error;
      dom.jobError.textContent = job.error || '';
      dom.jobLog.hidden = true;
      dom.cancelBtn.hidden = true;
      dom.jobMessage.textContent = i18n.t('jobCounts', {
        done: totals.done, skipped: totals.skipped, failed: totals.failed, total: totals.total,
      });
    }
    dom.jobFilesBody.innerHTML = (currentFiles() || []).map(function (file) {
      return '<tr>' +
        '<td>' + esc(file.filename) + '</td>' +
        '<td>' + (file.job_status ? '<span class="' + statusClass(file.job_status) + '">' +
          esc(statusLabel(file.job_status)) + '</span>' : '-') + '</td>' +
        '<td class="job-error">' + esc(file.job_error || '') + '</td>' +
        '</tr>';
    }).join('');
  }

  function updateStartButton() {
    var envReady = !!(state.overview && state.overview.env && state.overview.env.ready);
    var chosen = selectedFiles();
    var modelReady = modelInstalled(dom.modelSelect.value);
    var busy = taskRunning('transcribe') || taskRunning('install');
    dom.startBtn.disabled = !envReady || !modelReady || busy || chosen.length === 0;
    dom.startBtn.textContent = i18n.t(busy ? 'starting' : 'start');
  }

  // ---------------------------------------------------------------- 轮询

  function schedulePoll() {
    if (state.pollTimer) return;
    state.pollTimer = window.setTimeout(function () {
      state.pollTimer = null;
      pollTasks();
    }, POLL_MS);
  }

  function pollTasks() {
    if (state.pollBusy) {
      schedulePoll();
      return;
    }
    state.pollBusy = true;
    getJson('tasks').then(function (resp) {
      if (!resp || resp.err !== 'ok') return;
      var incoming = resp.data.tasks || {};
      var finished = [];
      ['install', 'download', 'transcribe'].forEach(function (kind) {
        var before = state.tasks[kind];
        var after = incoming[kind];
        if (before && before.status === 'running' && after && after.status !== 'running') {
          finished.push({ kind: kind, task: after });
        }
      });
      state.tasks = incoming;
      if (finished.length) handleFinished(finished);
      renderEnv();
      renderModels();
      if (state.book) renderJob();
      updateStartButton();
      if (taskRunning('transcribe') && state.bookId) {
        var now = Date.now();
        if (now - state.lastBookRefresh >= BOOK_REFRESH_MS) {
          state.lastBookRefresh = now;
          openBook(state.bookId, false);
        }
      }
    }).catch(function (err) {
      console.warn('poll tasks failed', err);
    }).then(function () {
      state.pollBusy = false;
      if (anyRunning()) schedulePoll();
    });
  }

  function handleFinished(finished) {
    finished.forEach(function (item) {
      var task = item.task;
      if (task.status === 'completed') {
        notify(i18n.t('taskDone', { kind: i18n.t('taskKind.' + item.kind) }), 'success');
      } else if (task.status === 'cancelled') {
        notify(i18n.t('taskCancelled', { kind: i18n.t('taskKind.' + item.kind) }), 'warning');
      } else {
        notify(task.error || i18n.t('taskFailed', { kind: i18n.t('taskKind.' + item.kind) }), 'error');
      }
    });
    refreshOverview();
    if (state.booksLoaded) refreshBooks();
    if (state.bookId) openBook(state.bookId, false);
  }

  // ---------------------------------------------------------------- 事件

  function collectConfig() {
    return {
      model: dom.modelSelect.value,
      language: dom.languageSelect.value,
      compute_type: dom.computeSelect.value,
      cpu_threads: parseInt(dom.threadsInput.value, 10) || 2,
      beam_size: parseInt(dom.beamInput.value, 10) || 5,
      initial_prompt: dom.promptInput.value || '',
      chunk_enabled: dom.chunkCheck.checked,
      pip_index: dom.pipInput.value.trim(),
      hf_endpoint: dom.hfInput.value.trim(),
    };
  }

  function saveEndpointConfig() {
    return postJson('config', {
      pip_index: dom.pipInput.value.trim(),
      hf_endpoint: dom.hfInput.value.trim(),
    }).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        notify(errText(resp), 'error');
        return;
      }
      if (state.overview) state.overview.config = resp.data.config;
      notify(i18n.t('configSaved'), 'success');
    });
  }

  function saveConfig(showNotify) {
    return postJson('config', collectConfig()).then(function (resp) {
      if (!resp || resp.err !== 'ok') {
        notify(errText(resp), 'error');
        return;
      }
      if (state.overview) state.overview.config = resp.data.config;
      if (showNotify) notify(i18n.t('configSaved'), 'success');
    });
  }

  function loadOverviewAndBooks() {
    return refreshOverview().then(function () {
      return refreshBooks();
    }).then(function () {
      if (state.overview && state.overview.latest_job &&
          state.overview.latest_job.status === 'interrupted' &&
          !state.bookId && state.overview.latest_job.book_id) {
        openBook(state.overview.latest_job.book_id, true);
      }
      if (anyRunning()) schedulePoll();
    });
  }

  function bindEvents() {
    dom.refreshEnv.addEventListener('click', function () {
      refreshOverview().then(function () { notify(i18n.t('refreshed'), 'success'); });
    });
    dom.refreshBooks.addEventListener('click', function () { refreshBooks(); });
    dom.installBtn.addEventListener('click', function () {
      dom.installBtn.disabled = true;
      postJson('install', { pip_index: dom.pipInput.value.trim() }).then(function (resp) {
        if (!resp || resp.err !== 'ok') {
          notify(errText(resp), 'error');
          dom.installBtn.disabled = false;
          return;
        }
        state.tasks.install = resp.data.task;
        renderEnv();
        schedulePoll();
      }).catch(function (err) {
        notify(String(err), 'error');
        dom.installBtn.disabled = false;
      });
    });
    dom.installCancel.addEventListener('click', function () {
      postJson('cancel', { kind: 'install' }).then(function (resp) {
        if (!resp || resp.err !== 'ok') notify(errText(resp), 'error');
      });
    });
    dom.downloadCancel.addEventListener('click', function () {
      postJson('cancel', { kind: 'download' }).then(function (resp) {
        if (!resp || resp.err !== 'ok') notify(errText(resp), 'error');
      });
    });
    dom.saveEndpoint.addEventListener('click', function () { saveEndpointConfig(); });
    dom.purgeBtn.addEventListener('click', function () {
      var models = (state.overview && state.overview.models) || [];
      var modelsSize = models.reduce(function (total, model) {
        return total + (model.installed ? Number(model.size || 0) : 0);
      }, 0);
      var depsSize = Number((state.overview && state.overview.env && state.overview.env.size) || 0);
      if (!window.confirm(i18n.t('confirmPurge', {
        deps: formatBytes(depsSize), models: formatBytes(modelsSize),
      }))) return;
      state.purging = true;
      renderEnv();
      postJson('purge', { deps: true, models: true }).then(function (resp) {
        state.purging = false;
        if (!resp || resp.err !== 'ok') {
          notify(errText(resp), 'error');
          renderEnv();
          return;
        }
        notify(i18n.t('purged', { size: formatBytes(resp.data.freed || 0) }), 'success');
        refreshOverview().then(function () {
          if (state.bookId) openBook(state.bookId, false);
        });
      }).catch(function (err) {
        state.purging = false;
        renderEnv();
        notify(String(err), 'error');
      });
    });
    dom.modelsBody.addEventListener('click', function (event) {
      var button = event.target.closest('button[data-action]');
      if (!button) return;
      var name = button.getAttribute('data-name');
      if (button.getAttribute('data-action') === 'download') {
        postJson('model_download', { name: name, endpoint: dom.hfInput.value.trim() }).then(function (resp) {
          if (!resp || resp.err !== 'ok') {
            notify(errText(resp), 'error');
            return;
          }
          state.tasks.download = resp.data.task;
          renderModels();
          renderEnv();
          schedulePoll();
        });
      } else if (button.getAttribute('data-action') === 'delete') {
        if (!window.confirm(i18n.t('confirmDeleteModel', { name: name }))) return;
        postJson('model_delete', { name: name }).then(function (resp) {
          if (!resp || resp.err !== 'ok') {
            notify(errText(resp), 'error');
            return;
          }
          refreshOverview();
        });
      }
    });
    dom.bookSearch.addEventListener('input', renderBooks);
    dom.onlyMissing.addEventListener('change', renderBooks);
    dom.booksBody.addEventListener('click', function (event) {
      var button = event.target.closest('button[data-book-id]');
      if (!button) return;
      openBook(parseInt(button.getAttribute('data-book-id'), 10), true);
    });
    dom.modeSelect.addEventListener('change', function () {
      state.mode = dom.modeSelect.value;
      renderBook();
    });
    dom.selectAll.addEventListener('click', function () {
      currentFiles().forEach(function (file) { state.selected[file.filename] = true; });
      renderBook();
    });
    dom.fileBody.addEventListener('change', function (event) {
      var input = event.target.closest('input[data-file]');
      if (!input) return;
      if (input.checked) state.selected[input.getAttribute('data-file')] = true;
      else delete state.selected[input.getAttribute('data-file')];
      var chosen = selectedFiles();
      dom.selectCount.textContent = i18n.t('selectedCount', { n: chosen.length });
      updateStartButton();
    });
    dom.saveConfig.addEventListener('click', function () { saveConfig(true); });
    dom.startBtn.addEventListener('click', function () {
      var chosen = selectedFiles();
      if (!chosen.length) {
        notify(i18n.t('noSelection'), 'warning');
        return;
      }
      var payload = collectConfig();
      payload.book_id = state.bookId;
      payload.mode = state.mode;
      payload.files = chosen;
      dom.startBtn.disabled = true;
      postJson('start', payload).then(function (resp) {
        if (!resp || resp.err !== 'ok') {
          notify(errText(resp), 'error');
          updateStartButton();
          return;
        }
        state.tasks.transcribe = resp.data.task;
        openBook(state.bookId, false);
        schedulePoll();
      }).catch(function (err) {
        notify(String(err), 'error');
        updateStartButton();
      });
    });
    dom.cancelBtn.addEventListener('click', function () {
      postJson('cancel', { kind: 'transcribe' }).then(function (resp) {
        if (!resp || resp.err !== 'ok') notify(errText(resp), 'error');
      });
    });
  }

  function refreshBook() {
    if (!state.bookId) return;
    openBook(state.bookId, false);
  }

  // ---------------------------------------------------------------- 启动

  bindEvents();
  i18n.ready.then(function () {
    loadOverviewAndBooks();
  });
  i18n.onChange(function () {
    if (!state.overview) return;
    renderEnv();
    renderModels();
    renderBooks();
    if (state.book) {
      renderBook();
    }
    updateStartButton();
  });
})();
