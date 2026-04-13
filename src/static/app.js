/* ============================================================
   RBRCS Command Center — Frontend Logic v2
   ============================================================ */

const API = '';
const POLL_INTERVAL = 15000;

let pollTimer = null;
let isConnected = false;

// ── Utilities ──────────────────────────────────────────────

function timeAgo(dateStr) {
  if (!dateStr) return '—';
  const d = new Date(dateStr.replace(' ', 'T'));
  const now = new Date();
  const sec = Math.floor((now - d) / 1000);
  if (isNaN(sec) || sec < 0) return dateStr;
  if (sec < 60) return sec + 's ago';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
  if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
  return Math.floor(sec / 86400) + 'd ago';
}

function formatBytes(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function escapeHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ── Toast System ───────────────────────────────────────────

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const icons = { success: '✓', error: '✕', info: 'ℹ' };
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <span class="toast-icon">${icons[type] || 'ℹ'}</span>
    <span class="toast-text">${escapeHtml(message)}</span>
    <button class="toast-close" onclick="this.parentElement.remove()">✕</button>
  `;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('removing');
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

// ── API Calls ──────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  try {
    options.credentials = 'same-origin';
    const res = await fetch(API + path, options);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    setConnected(true);
    return data;
  } catch (e) {
    if (!options.method || options.method === 'GET') setConnected(false);
    throw e;
  }
}

function setConnected(state) {
  if (isConnected === state) return;
  isConnected = state;
  const dot = document.getElementById('conn-dot');
  const label = document.getElementById('conn-label');
  if (dot) dot.className = state ? 'connection-dot' : 'connection-dot offline';
  if (label) label.textContent = state ? 'Connected' : 'Disconnected';
}

// ── Navigation (SPA) ───────────────────────────────────────

const viewTitles = {
  'dashboard': ['Dashboard Overview', 'Dashboard'],
  'fleet': ['Router Fleet Management', 'Fleet'],
  'add-router': ['Deploy New Router', 'Deploy'],
  'terminal': ['Configuration Terminal', 'Terminal'],
  'compliance': ['Security & Compliance Audit', 'Compliance']
};

function switchView(viewId) {
  document.querySelectorAll('.nav-link').forEach(link => {
    link.classList.toggle('active', link.dataset.view === viewId);
  });

  const [title, crumb] = viewTitles[viewId] || ['Dashboard', 'Dashboard'];
  document.getElementById('view-title').textContent = title;
  document.getElementById('view-breadcrumb').textContent = crumb;

  document.querySelectorAll('.view').forEach(view => {
    view.classList.toggle('active', view.id === `view-${viewId}`);
  });

  if (viewId === 'compliance') refreshCompliance();
}

// ── Clock ──────────────────────────────────────────────────

function updateClock() {
  const el = document.getElementById('header-clock');
  if (el) el.textContent = new Date().toLocaleTimeString('en-GB', { hour12: false });
}
setInterval(updateClock, 1000);
updateClock();

// ── Dashboard Refresh ──────────────────────────────────────

async function refreshDashboard() {
  try {
    const [stats, jobs, retention, routers] = await Promise.all([
      apiFetch('/api/stats'),
      apiFetch('/api/jobs'),
      apiFetch('/api/retention-stats'),
      apiFetch('/api/routers')
    ]);
    renderStats(stats);
    renderRouters(routers);
    renderEvents(stats.recent_events || []);
    renderJobs(jobs);
    renderRetention(retention);
  } catch (e) {
    console.warn('Dashboard refresh failed:', e.message);
  }
}

// ── Render: Stats Bar ──────────────────────────────────────

function renderStats(stats) {
  document.getElementById('stat-total').textContent = stats.total_routers || 0;
  document.getElementById('stat-online').textContent = stats.online_routers || 0;
  document.getElementById('stat-backups').textContent = stats.total_backups || 0;
  document.getElementById('stat-storage').innerHTML = formatBytes(stats.total_storage || 0);
}

// ── Render: Router Fleet ───────────────────────────────────

function renderRouters(routers) {
  const tbody = document.getElementById('router-tbody');
  const fleetTbody = document.getElementById('fleet-tbody');
  const grid = document.getElementById('pc-routers-grid');

  // Terminal router grid (checkboxes with search data)
  const prevChecked = Array.from(document.querySelectorAll('.pc-router-cb:checked')).map(cb => cb.value);
  if (grid) {
    if (!routers.length) {
      grid.innerHTML = '<div style="color:var(--text-muted);padding:10px;grid-column:1/-1;">No routers available</div>';
    } else {
      grid.innerHTML = routers.map(r => `
        <label class="router-selector-item" data-name="${escapeHtml(r.name).toLowerCase()}" data-host="${escapeHtml(r.host).toLowerCase()}">
          <input type="checkbox" class="pc-router-cb" value="${r.id}" ${prevChecked.includes(r.id) ? 'checked' : ''} onchange="updateRouterCount()">
          <span class="router-selector-dot ${r.status || 'unknown'}"></span>
          <span class="router-selector-name">${escapeHtml(r.name)}</span>
          <span class="router-selector-host">${escapeHtml(r.host)}</span>
        </label>
      `).join('');
    }
    updateRouterCount();
  }

  const buildRow = (r) => {
    const safeName = escapeHtml(r.name).replace(/'/g, "&#39;").replace(/"/g, "&quot;");
    const safeId = escapeHtml(r.id).replace(/'/g, "&#39;").replace(/"/g, "&quot;");
    return `
    <tr>
      <td>
        <div class="router-name">${escapeHtml(r.name)}</div>
        <div class="router-host">${escapeHtml(r.host)}:${r.port || 22}</div>
      </td>
      <td><span class="badge badge-${r.status || 'unknown'}">${r.status || 'unknown'}</span></td>
      <td style="font-size:12px;color:var(--text-secondary)">${escapeHtml(r.device_type)}</td>
      <td style="font-family:'JetBrains Mono',monospace;font-size:12px;">${r.backup_count || 0}</td>
      <td style="font-size:12px;color:var(--text-muted)">${timeAgo(r.last_backup)}</td>
      <td>
        <div class="action-cell">
          <button class="btn btn-sm btn-success" onclick="triggerBackup('${safeId}')" title="Backup Now">⬇ Backup</button>
          <button class="btn btn-sm" onclick="viewHistory('${safeId}', '${safeName}')" title="History">📋</button>
          <button class="btn btn-sm btn-danger" onclick="deleteRouter('${safeId}', '${safeName}')" title="Delete">🗑</button>
        </div>
      </td>
    </tr>
  `;
  };

  if (!routers.length) {
    const emptyRow = `<tr><td colspan="6"><div class="empty-state">
      <div class="empty-state-icon">📡</div>
      <div class="empty-state-text">No routers configured. Deploy one to get started.</div>
    </div></td></tr>`;
    tbody.innerHTML = emptyRow;
    if (fleetTbody) fleetTbody.innerHTML = emptyRow;
    return;
  }

  const rows = routers.map(buildRow).join('');
  tbody.innerHTML = rows;
  if (fleetTbody) fleetTbody.innerHTML = rows;
}

// ── Render: Events ─────────────────────────────────────────

function renderEvents(events) {
  const list = document.getElementById('event-list');
  if (!events.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-state-icon">📭</div><div class="empty-state-text">No events yet</div></div>`;
    return;
  }
  list.innerHTML = events.map(e => `
    <div class="event-item">
      <div class="event-header">
        <span class="event-severity ${e.severity || 'info'}"></span>
        <span class="event-type">${escapeHtml(e.event_type)}</span>
        <span class="event-time">${timeAgo(e.timestamp)}</span>
      </div>
      <div class="event-message">${escapeHtml(e.message)}</div>
      ${e.router_name ? `<div class="event-router">↳ ${escapeHtml(e.router_name)}</div>` : ''}
    </div>
  `).join('');
}

// ── Render: Jobs & Retention ───────────────────────────────

function renderJobs(jobs) {
  const tbody = document.getElementById('jobs-tbody');
  if (!jobs.length) {
    tbody.innerHTML = `<tr><td colspan="2" style="text-align:center;padding:20px;color:var(--text-muted)">No scheduled jobs</td></tr>`;
    return;
  }
  tbody.innerHTML = jobs.map(j => `
    <tr>
      <td class="job-name">${escapeHtml(j.name)}</td>
      <td class="job-next">${j.next_run ? timeAgo(j.next_run) : '—'}</td>
    </tr>
  `).join('');
}

function renderRetention(r) {
  document.getElementById('ret-unique').textContent = r.unique_configs || 0;
  document.getElementById('ret-dedup').textContent = r.duplicates_avoided || 0;
}

// ── Actions ────────────────────────────────────────────────

async function triggerBackup(routerId) {
  showToast('Starting backup…', 'info');
  try {
    const res = await apiFetch(`/api/backup/${routerId}`);
    if (res.success) showToast(res.message || 'Backup complete', 'success');
    else showToast(res.message || 'Backup failed', 'error');
    refreshDashboard();
  } catch (e) {
    showToast('Backup failed: ' + e.message, 'error');
  }
}

async function deleteRouter(routerId, routerName) {
  // Decode HTML entities from safeName for the confirm dialog
  const decodeHtml = (html) => {
    var txt = document.createElement("textarea");
    txt.innerHTML = html;
    return txt.value;
  };
  if (!confirm(`🧨 Delete router "${decodeHtml(routerName)}"?\n\nThis removes all history and backups permanently.`)) return;
  showToast('Deleting router...', 'info');
  try {
    const res = await apiFetch(`/api/routers/${encodeURIComponent(decodeHtml(routerId))}`, { method: 'DELETE' });
    if (res.success) showToast('Router deleted', 'success');
    refreshDashboard();
  } catch (e) {
    showToast('Delete failed: ' + e.message, 'error');
  }
}

// ── Add Router Form ────────────────────────────────────────

document.getElementById('add-router-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const payload = {
    id: document.getElementById('ar-id').value.trim(),
    name: document.getElementById('ar-name').value.trim(),
    host: document.getElementById('ar-host').value.trim(),
    port: parseInt(document.getElementById('ar-port').value) || 22,
    device_type: document.getElementById('ar-type').value,
    username: document.getElementById('ar-user').value.trim() || null,
    password: document.getElementById('ar-pass').value || null,
    enable_password: document.getElementById('ar-enable').value || null
  };
  Object.keys(payload).forEach(k => payload[k] === null && delete payload[k]);

  showToast('Saving router...', 'info');
  try {
    const res = await apiFetch('/api/routers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.success) {
      showToast('Router added successfully', 'success');
      e.target.reset();
      refreshDashboard();
      switchView('fleet');
    }
  } catch (err) {
    showToast('Failed to add router: ' + err.message, 'error');
  }
});

async function testConnection() {
  const payload = {
    id: document.getElementById('ar-id').value.trim() || 'test-temp',
    host: document.getElementById('ar-host').value.trim(),
    port: parseInt(document.getElementById('ar-port').value) || 22,
    device_type: document.getElementById('ar-type').value,
    username: document.getElementById('ar-user').value.trim() || 'admin',
    password: document.getElementById('ar-pass').value || '',
    enable_password: document.getElementById('ar-enable').value || ''
  };

  if (!payload.host) { showToast('Host/IP is required to test!', 'error'); return; }

  showToast('Testing connection...', 'info');
  try {
    const res = await apiFetch('/api/routers/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.success) showToast('✅ Connection successful!', 'success');
    else showToast('❌ Failed: ' + res.message, 'error');
  } catch (err) {
    showToast('Test failed: ' + err.message, 'error');
  }
}

// ── Mass Import ────────────────────────────────────────────

function downloadCsvTemplate() {
  const csv = "id,name,host,port,device_type,username,password\nhq-rt-01,HQ Router,192.168.1.1,22,mikrotik_routeros,admin,ChangeMe123\n";
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'rbrcs_routers_template.csv';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

document.getElementById('bulk-upload-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const fileInput = document.getElementById('bulk-file');
  if (!fileInput.files.length) return;

  const btn = document.getElementById('bulk-submit-btn');
  btn.disabled = true;
  btn.textContent = '⌛ Processing...';

  const formData = new FormData();
  formData.append('file', fileInput.files[0]);

  showToast('Uploading and validating routers...', 'info');
  try {
    const res = await fetch(API + '/api/routers/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (res.ok && data.success) {
      showImportSummary(data);
      e.target.reset();
      refreshDashboard();
    } else {
      showToast('Import failed: ' + (data.error || 'Unknown error'), 'error');
    }
  } catch (err) {
    showToast('Connection failed: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '📥 Import Routers';
  }
});

function showImportSummary(data) {
  const { summary, results, imported_ids } = data;
  let html = `
    <div class="import-summary-header">
      <div class="summary-stat">
        <div class="stat-value">${summary.total}</div>
        <div class="stat-label">Total Rows</div>
      </div>
      <div class="summary-stat" style="color:var(--accent-green)">
        <div class="stat-value" style="color:var(--accent-green)">${summary.success}</div>
        <div class="stat-label">Success</div>
      </div>
      <div class="summary-stat" style="color:var(--accent-red)">
        <div class="stat-value" style="color:var(--accent-red)">${summary.failed}</div>
        <div class="stat-label">Failed</div>
      </div>
    </div>
    <div style="max-height:260px;overflow-y:auto;border:1px solid var(--border);border-radius:var(--radius);">
      <table class="data-table" style="margin:0;font-size:12px;">
        <thead><tr><th>Row</th><th>ID</th><th>Status</th><th>Message</th></tr></thead>
        <tbody>
          ${results.map(r => `
            <tr>
              <td>${r.row}</td>
              <td style="font-family:monospace;">${r.id || '—'}</td>
              <td><span class="badge badge-${r.status === 'success' ? 'online' : 'error'}">${r.status}</span></td>
              <td style="color:var(--text-muted)">${escapeHtml(r.message)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;

  if (imported_ids && imported_ids.length > 0) {
    html += `
      <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:20px;">
        <button class="btn" onclick="closeModal()">Close</button>
        <button class="btn btn-primary" onclick="redirectToTerminal(${JSON.stringify(imported_ids)})">⚡ Configure These Routers</button>
      </div>
    `;
  } else {
    html += `<div style="margin-top:20px;"><button class="btn" style="width:100%" onclick="closeModal()">Close</button></div>`;
  }

  openModal('📥 Import Results', html);
}

function redirectToTerminal(routerIds) {
  closeModal();
  switchView('terminal');
  setTimeout(() => {
    document.querySelectorAll('.pc-router-cb').forEach(cb => {
      cb.checked = routerIds.includes(cb.value);
    });
    updateRouterCount();
    showToast(`Pre-selected ${routerIds.length} routers for configuration.`, 'info');
  }, 150);
}

// ── Mass Push ──────────────────────────────────────────────

function updateRouterCount() {
  const total = document.querySelectorAll('.pc-router-cb').length;
  const checked = document.querySelectorAll('.pc-router-cb:checked').length;
  const badge = document.getElementById('router-count-badge');
  if (badge) badge.textContent = `${checked} selected`;
  const label = document.getElementById('router-dropdown-label');
  if (label) {
    if (checked === 0) label.textContent = 'Select routers...';
    else if (checked === total) label.textContent = `All ${total} routers selected`;
    else label.textContent = `${checked} of ${total} routers selected`;
  }
}

function toggleRouterDropdown() {
  const dd = document.getElementById('router-dropdown');
  if (dd) dd.classList.toggle('open');
}

// Close dropdown when clicking outside
document.addEventListener('click', (e) => {
  const dd = document.getElementById('router-dropdown');
  if (dd && dd.classList.contains('open') && !dd.contains(e.target)) {
    dd.classList.remove('open');
  }
});

function toggleAllRouters() {
  const visible = document.querySelectorAll('.router-selector-item:not(.hidden) .pc-router-cb');
  const allChecked = Array.from(visible).every(cb => cb.checked);
  visible.forEach(cb => cb.checked = !allChecked);
  document.getElementById('btn-toggle-routers').textContent = allChecked ? 'Select All' : 'Deselect All';
  updateRouterCount();
}

function clearTerminal() {
  const termOut = document.getElementById('terminal-out');
  if (termOut) {
    termOut.textContent = 'RBRCS Terminal v2.0\nReady. Select routers and enter commands to execute.\n' + '─'.repeat(52) + '\n';
  }
}

document.getElementById('push-config-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const routerIds = Array.from(document.querySelectorAll('.pc-router-cb:checked')).map(cb => cb.value);
  const commands = document.getElementById('pc-commands').value;
  const termOut = document.getElementById('terminal-out');
  const btn = document.getElementById('btn-push-exec');

  if (!routerIds.length) { showToast('Select at least one router!', 'error'); return; }
  if (!commands.trim()) return;
  if (!confirm(`⚠️ Push configuration to ${routerIds.length} live device(s)?`)) return;

  btn.disabled = true;
  termOut.textContent = `⚡ Targeting ${routerIds.length} routers...\n🚀 Initializing parallel execution engine...\n\n`;

  try {
    const res = await apiFetch('/api/routers/mass-push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ router_ids: routerIds, commands: commands })
    });

    if (res.results) {
      termOut.textContent = `✅ Execution complete — ${routerIds.length} routers.\n${'═'.repeat(52)}\n\n`;
      for (const [rid, rdata] of Object.entries(res.results)) {
        const icon = rdata.success ? '🟢' : '🔴';
        termOut.textContent += `${icon} [${rid}] ${rdata.success ? 'SUCCESS' : 'FAILED'}\n`;
        termOut.textContent += `${'─'.repeat(52)}\n`;
        termOut.textContent += `${(rdata.output || 'No output.').trim()}\n`;
        termOut.textContent += `${'═'.repeat(52)}\n\n`;
      }
      showToast(`Push complete: ${routerIds.length} routers processed`, 'success');
      termOut.scrollTop = termOut.scrollHeight;
    } else {
      showToast('Push rejected by server', 'error');
      termOut.textContent += '\n❌ Server rejected the request.';
    }
  } catch (err) {
    termOut.textContent += `\n❌ Error: ${err.message}`;
    showToast('Execution failed: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
  }
});

// ── Nav Links ──────────────────────────────────────────────

document.querySelectorAll('.nav-link').forEach(link => {
  link.addEventListener('click', (e) => {
    e.preventDefault();
    switchView(e.currentTarget.dataset.view);
  });
});

// ── History Modal ──────────────────────────────────────────

async function viewHistory(routerId, routerName) {
  openModal(`📋 Backup History — ${routerName}`, '<div class="skeleton" style="height:200px"></div>');
  try {
    const history = await apiFetch(`/api/routers/${routerId}/history`);
    if (!history.length) {
      setModalBody('<div class="empty-state"><div class="empty-state-icon">📭</div><div class="empty-state-text">No backups found</div></div>');
      return;
    }
    setModalBody(`
      <ul class="history-list">
        ${history.map(h => `
          <li class="history-item">
            <div class="history-meta">
              <span class="history-id">#${h.id} — ${h.change_type || 'auto'}</span>
              <span class="history-time">${timeAgo(h.timestamp)} · ${h.timestamp || ''}</span>
            </div>
            <span class="history-size">${formatBytes(h.config_size)}</span>
            <div class="history-actions">
              <button class="btn btn-sm" onclick="viewConfig('${routerId}', ${h.id})">👁 View</button>
              <button class="btn btn-sm btn-danger" onclick="restoreSpecific('${routerId}', ${h.id}, '${escapeHtml(routerName)}')">⟲ Restore</button>
            </div>
          </li>
        `).join('')}
      </ul>
    `);
  } catch (e) {
    setModalBody(`<div class="empty-state"><div class="empty-state-text">Failed: ${e.message}</div></div>`);
  }
}

// ── Config Viewer ──────────────────────────────────────────

async function viewConfig(routerId, configId) {
  openModal(`🔧 Config #${configId}`, '<div class="skeleton" style="height:300px"></div>');
  try {
    const cfg = await apiFetch(`/api/routers/${routerId}/config/${configId}`);
    setModalBody(`
      <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <span style="font-size:11px;color:var(--text-muted)">Hash: <code>${cfg.config_hash || '—'}</code> · Size: ${formatBytes(cfg.config_size)} · ${cfg.timestamp || ''}</span>
        <button class="btn btn-sm" onclick="diffWithPrevious('${routerId}', ${configId})">📊 Diff vs Previous</button>
      </div>
      <div class="config-viewer"><pre>${escapeHtml(cfg.config_text || 'No config data')}</pre></div>
    `);
  } catch (e) {
    setModalBody(`<div class="empty-state"><div class="empty-state-text">Failed: ${e.message}</div></div>`);
  }
}

async function diffWithPrevious(routerId, configId) {
  openModal(`📊 Config Diff #${configId}`, '<div class="skeleton" style="height:300px"></div>');
  try {
    const data = await apiFetch(`/api/config/diff?router_id=${routerId}&config_id=${configId}`);
    if (data.error) {
      setModalBody(`<div class="empty-state"><div class="empty-state-text">${escapeHtml(data.error)}</div></div>`);
      return;
    }
    const diffLines = (data.diff || '').split('\n').map(line => {
      if (line.startsWith('+') && !line.startsWith('+++')) return `<span class="diff-add">${escapeHtml(line)}</span>`;
      if (line.startsWith('-') && !line.startsWith('---')) return `<span class="diff-del">${escapeHtml(line)}</span>`;
      if (line.startsWith('@@')) return `<span class="diff-hdr">${escapeHtml(line)}</span>`;
      return escapeHtml(line);
    }).join('\n');
    setModalBody(`
      <div style="margin-bottom:12px;font-size:11px;color:var(--text-muted)">
        Comparing #${data.current_id} vs #${data.previous_id} ·
        <span style="color:var(--accent-green)">+${data.additions}</span> /
        <span style="color:var(--accent-red)">-${data.deletions}</span> lines
      </div>
      <div class="config-viewer"><pre>${diffLines}</pre></div>
    `);
  } catch (e) {
    setModalBody(`<div class="empty-state"><div class="empty-state-text">Diff failed: ${e.message}</div></div>`);
  }
}

async function restoreSpecific(routerId, configId, routerName) {
  if (!confirm(`Restore "${routerName}" to backup #${configId}?`)) return;
  showToast('Starting restore…', 'info');
  try {
    const res = await apiFetch(`/api/restore/${routerId}?config_id=${configId}`);
    showToast(res.message || (res.success ? 'Restored' : 'Failed'), res.success ? 'success' : 'error');
    closeModal();
    refreshDashboard();
  } catch (e) {
    showToast('Restore failed: ' + e.message, 'error');
  }
}

// ── Modal Helpers ──────────────────────────────────────────

function openModal(title, body) {
  document.getElementById('modal-title').innerHTML = title;
  document.getElementById('modal-body').innerHTML = body;
  document.getElementById('modal-overlay').classList.add('active');
}

function setModalBody(html) {
  document.getElementById('modal-body').innerHTML = html;
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('active');
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
});

// ── Auth ───────────────────────────────────────────────────

document.getElementById('login-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const user = document.getElementById('login-user').value;
  const pass = document.getElementById('login-pass').value;
  const errorEl = document.getElementById('login-error');
  errorEl.textContent = '';

  try {
    const res = await fetch(API + '/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, password: pass })
    });
    if (res.ok) {
      document.getElementById('login-overlay').style.display = 'none';
      document.getElementById('app-layout').style.display = 'flex';
      refreshDashboard();
      pollTimer = setInterval(refreshDashboard, POLL_INTERVAL);
    } else {
      const data = await res.json();
      errorEl.textContent = data.error || 'Invalid credentials';
    }
  } catch (err) {
    errorEl.textContent = 'Unable to connect to server';
  }
});

async function logout() {
  await fetch(API + '/api/logout', { method: 'POST' });
  location.reload();
}

// ── Compliance ─────────────────────────────────────────────

async function refreshCompliance() {
  const tbody = document.getElementById('compliance-tbody');
  tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:24px;">🔄 Scanning…</td></tr>';
  try {
    const routers = await apiFetch('/api/routers');
    if (!routers.length) {
      tbody.innerHTML = '<tr><td colspan="6"><div class="empty-state"><div class="empty-state-text">No routers to scan</div></div></td></tr>';
      return;
    }

    let html = '';
    for (const r of routers) {
      try {
        const rep = await apiFetch(`/api/routers/${r.id}/compliance`);
        html += `<tr>
          <td><div class="router-name">${escapeHtml(r.name)}</div></td>
          <td><span class="grade-badge grade-${rep.grade}">${rep.grade}</span></td>
          <td style="font-family:'JetBrains Mono',monospace;">${rep.score}/100</td>
          <td style="color:var(--accent-green)">${rep.passed ? rep.passed.length : 0}</td>
          <td style="color:var(--accent-red)">${rep.failed ? rep.failed.length : 0}</td>
          <td><button class="btn btn-sm" onclick="viewComplianceReport('${r.id}', '${escapeHtml(r.name)}')">View Report</button></td>
        </tr>`;
      } catch (e) {
        html += `<tr><td colspan="6" style="color:var(--accent-red)">Failed: ${escapeHtml(r.name)}</td></tr>`;
      }
    }
    tbody.innerHTML = html;
  } catch (e) {
    showToast('Compliance scan failed: ' + e.message, 'error');
  }
}

async function viewComplianceReport(routerId, routerName) {
  openModal(`🛡️ Security Report — ${routerName}`, '<div class="skeleton" style="height:300px"></div>');
  try {
    const rep = await apiFetch(`/api/routers/${routerId}/compliance`);
    let html = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;">
        <div>
          <div style="font-size:14px;color:var(--text-secondary);margin-bottom:4px;">Security Score</div>
          <div style="font-size:28px;font-weight:800;">${rep.score} / 100</div>
        </div>
        <span class="grade-badge grade-${rep.grade}" style="width:48px;height:48px;font-size:22px;">${rep.grade}</span>
      </div>
    `;

    if (rep.failed && rep.failed.length > 0) {
      html += `<div style="margin-bottom:8px;font-weight:600;font-size:13px;color:var(--accent-red);">🔥 Failed Checks (${rep.failed.length})</div>`;
      html += `<ul style="list-style:none;margin-bottom:20px;">`;
      rep.failed.forEach(f => {
        html += `<li style="padding:8px 12px;background:rgba(239,68,68,.05);border-left:3px solid var(--accent-red);margin-bottom:6px;border-radius:0 var(--radius-sm) var(--radius-sm) 0;font-size:12px;color:var(--text-secondary);">
          <strong style="color:var(--accent-red)">[${escapeHtml(f.id)}]</strong> ${escapeHtml(f.description)}
        </li>`;
      });
      html += `</ul>`;
    }

    if (rep.passed && rep.passed.length > 0) {
      html += `<div style="margin-bottom:8px;font-weight:600;font-size:13px;color:var(--accent-green);">✓ Passed Checks (${rep.passed.length})</div>`;
      html += `<ul style="list-style:none;">`;
      rep.passed.forEach(p => {
        html += `<li style="padding:6px 12px;font-size:12px;color:var(--text-muted);border-left:2px solid rgba(16,185,129,.3);margin-bottom:4px;">
          [${escapeHtml(p.id)}] ${escapeHtml(p.description)}
        </li>`;
      });
      html += `</ul>`;
    }

    setModalBody(html);
  } catch (e) {
    setModalBody('Failed to load report: ' + e.message);
  }
}

// ── Init ───────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  try {
    const auth = await fetch(API + '/api/auth/status').then(r => r.json());
    if (auth.enabled && !auth.logged_in) {
      document.getElementById('login-overlay').style.display = 'flex';
      return;
    }
  } catch (e) {
    console.warn('Auth check failed, assuming no auth', e);
  }

  document.getElementById('login-overlay').style.display = 'none';
  document.getElementById('app-layout').style.display = 'flex';
  refreshDashboard();
  pollTimer = setInterval(refreshDashboard, POLL_INTERVAL);
});

// ── Router Search Filter ───────────────────────────────────

document.getElementById('router-search')?.addEventListener('input', (e) => {
  const query = e.target.value.toLowerCase().trim();
  document.querySelectorAll('.router-selector-item').forEach(item => {
    const name = item.dataset.name || '';
    const host = item.dataset.host || '';
    if (!query || name.includes(query) || host.includes(query)) {
      item.classList.remove('hidden');
    } else {
      item.classList.add('hidden');
    }
  });
});

// ── Import Dropzone Drag Effects ───────────────────────────

const dropzone = document.getElementById('import-dropzone');
if (dropzone) {
  ['dragenter', 'dragover'].forEach(evt => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add('drag-over');
    });
  });
  ['dragleave', 'drop'].forEach(evt => {
    dropzone.addEventListener(evt, () => {
      dropzone.classList.remove('drag-over');
    });
  });
}
