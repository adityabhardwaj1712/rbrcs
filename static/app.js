/* ============================================================
   RBRCS Dashboard — Frontend Logic
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
    const res = await fetch(API + path, options);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    setConnected(true);
    return data;
  } catch (e) {
    if(!options.method || options.method === 'GET') setConnected(false);
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

function switchView(viewId) {
  // Update nav links
  document.querySelectorAll('.nav-link').forEach(link => {
    link.classList.toggle('active', link.dataset.view === viewId);
  });
  
  // Title mapping
  const titles = {
    'dashboard': 'Dashboard Overview',
    'fleet': 'Router Fleet Management',
    'add-router': 'Deploy New Router',
    'terminal': 'Configuration Terminal',
    'compliance': 'Security & Compliance Audit'
  };
  document.getElementById('view-title').textContent = titles[viewId] || 'Dashboard';

  // Toggle views
  document.querySelectorAll('.view').forEach(view => {
    view.classList.toggle('active', view.id === `view-${viewId}`);
  });

  if (viewId === 'compliance') {
    refreshCompliance();
  }
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
  const select = document.getElementById('pc-router');
  
  // Update Dropdown
  const prevSelected = select.value;
  select.innerHTML = '<option value="">Select a router...</option>' + 
    routers.map(r => `<option value="${r.id}">${escapeHtml(r.name)} (${r.host})</option>`).join('');
  if (prevSelected) select.value = prevSelected;

  // Update Table
  if (!routers.length) {
    tbody.innerHTML = `<tr><td colspan="6"><div class="empty-state">
      <div class="empty-state-icon">📡</div>
      <div class="empty-state-text">No routers configured. Add one via the sidebar.</div>
    </div></td></tr>`;
    return;
  }
  tbody.innerHTML = routers.map(r => `
    <tr>
      <td>
        <div class="router-name">${escapeHtml(r.name)}</div>
        <div class="router-host">${escapeHtml(r.host)}:${r.port || 22}</div>
      </td>
      <td><span class="status-badge ${r.status || 'unknown'}">${r.status || 'unknown'}</span></td>
      <td>${escapeHtml(r.device_type)}</td>
      <td>${r.backup_count || 0}</td>
      <td>${timeAgo(r.last_backup)}</td>
      <td>
        <div class="action-cell">
          <button class="btn btn-sm btn-primary" onclick="triggerBackup('${r.id}')" title="Backup Now">⬇ Backup</button>
          <button class="btn btn-sm" onclick="viewHistory('${r.id}', '${escapeHtml(r.name)}')" title="View History">📋 History</button>
          <button class="btn btn-sm btn-danger" onclick="deleteRouter('${r.id}', '${escapeHtml(r.name)}')" title="Delete Router">🗑</button>
        </div>
      </td>
    </tr>
  `).join('');
}

// ── Render: Events, Jobs, Retention ────────────────────────

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

function renderJobs(jobs) {
  const tbody = document.getElementById('jobs-tbody');
  if (!jobs.length) {
    tbody.innerHTML = `<tr><td colspan="2" class="empty-state">No scheduled jobs</td></tr>`;
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

async function triggerRestore(routerId, routerName) {
  if (!confirm(`⚠️ Restore ${routerName} to golden/last-good config?\n\nThis will push config to the live device.`)) return;
  showToast('Starting restore…', 'info');
  try {
    const res = await apiFetch(`/api/restore/${routerId}`);
    if (res.success) showToast(res.message || 'Restore complete', 'success');
    else showToast(res.message || 'Restore failed', 'error');
    refreshDashboard();
  } catch (e) {
    showToast('Restore request failed: ' + e.message, 'error');
  }
}

async function deleteRouter(routerId, routerName) {
  if (!confirm(`🧨 DANGER: Delete router ${routerName}?\n\nThis removes all history, backups, and settings permanently.`)) return;
  showToast('Deleting router...', 'info');
  try {
    const res = await apiFetch(`/api/routers/${routerId}`, { method: 'DELETE' });
    if(res.success) showToast('Router deleted', 'success');
    refreshDashboard();
  } catch(e) {
    showToast('Delete failed: ' + e.message, 'error');
  }
}

// ── Form Submissions ───────────────────────────────────────

document.getElementById('add-router-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const payload = {
    id: document.getElementById('ar-id').value,
    name: document.getElementById('ar-name').value,
    host: document.getElementById('ar-host').value,
    port: parseInt(document.getElementById('ar-port').value),
    device_type: document.getElementById('ar-type').value,
    username: document.getElementById('ar-user').value || null,
    password: document.getElementById('ar-pass').value || null,
    enable_password: document.getElementById('ar-enable').value || null
  };
  
  // Clean up empty optional fields
  Object.keys(payload).forEach(k => payload[k] === null && delete payload[k]);

  showToast('Saving router...', 'info');
  try {
    const res = await apiFetch('/api/routers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if(res.success) {
      showToast('Router added successfully', 'success');
      e.target.reset();
      refreshDashboard();
      switchView('fleet');
    }
  } catch (err) {
    showToast('Failed to add router: ' + err.message, 'error');
  }
});

document.getElementById('push-config-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const routerId = document.getElementById('pc-router').value;
  const commands = document.getElementById('pc-commands').value;
  const termOut = document.getElementById('terminal-out');
  const btn = document.getElementById('btn-push-exec');
  
  if(!routerId || !commands.trim()) return;
  
  if(!confirm(`⚠️ Push this configuration to the live device?`)) return;

  btn.disabled = true;
  termOut.textContent = "Connecting and pushing config...\n";
  
  try {
    const res = await apiFetch(`/api/routers/${routerId}/push`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ commands })
    });
    termOut.textContent += (res.output || "No output returned.");
    if(res.success) showToast('Config pushed successfully', 'success');
    else showToast('Config push completed with errors', 'error');
  } catch (err) {
    termOut.textContent += `\nError: ${err.message}`;
    showToast('Failed to push config: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
  }
});

// ── Bind Nav Links ─────────────────────────────────────────

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
    setModalBody(`<div class="empty-state"><div class="empty-state-text">Failed to load history: ${e.message}</div></div>`);
  }
}

// ── Config Viewer ──────────────────────────────────────────

async function viewConfig(routerId, configId) {
  openModal(`🔧 Config #${configId}`, '<div class="skeleton" style="height:300px"></div>');
  try {
    const cfg = await apiFetch(`/api/routers/${routerId}/config/${configId}`);
    const configText = cfg.config_text || 'No config data';
    setModalBody(`
      <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center;">
        <span style="font-size:12px;color:var(--text-muted)">Hash: <code>${cfg.config_hash || '—'}</code> · Size: ${formatBytes(cfg.config_size)} · ${cfg.timestamp || ''}</span>
        <button class="btn btn-sm" onclick="diffWithPrevious('${routerId}', ${configId})">📊 Diff vs Previous</button>
      </div>
      <div class="config-viewer"><pre>${escapeHtml(configText)}</pre></div>
    `);
  } catch (e) {
    setModalBody(`<div class="empty-state"><div class="empty-state-text">Failed to load config: ${e.message}</div></div>`);
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
      <div style="margin-bottom:12px;font-size:12px;color:var(--text-muted)">
        Comparing config #${data.current_id} vs #${data.previous_id} ·
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
  if (!confirm(`Restore ${routerName} to backup #${configId}?`)) return;
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
  document.getElementById('modal-title').textContent = '';
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

document.addEventListener('click', (e) => {
  if (e.target.id === 'modal-overlay') closeModal();
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
});

// ── Auth & Compliance ──────────────────────────────────────

document.getElementById('login-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const user = document.getElementById('login-user').value;
  const pass = document.getElementById('login-pass').value;
  try {
    const res = await fetch(API + '/api/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: user, password: pass})
    });
    if (res.ok) {
      document.getElementById('login-overlay').style.display = 'none';
      document.getElementById('app-layout').style.display = 'flex';
      refreshDashboard();
      pollTimer = setInterval(refreshDashboard, POLL_INTERVAL);
    } else {
      document.getElementById('login-error').textContent = 'Invalid credentials';
    }
  } catch(e) {
    document.getElementById('login-error').textContent = 'Login server error';
  }
});

async function logout() {
  await fetch(API + '/api/logout', { method: 'POST' });
  location.reload();
}

async function refreshCompliance() {
  const tbody = document.getElementById('compliance-tbody');
  tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;">Scanning...</td></tr>';
  try {
    const routers = await apiFetch('/api/routers');
    if (!routers.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No routers to scan</td></tr>';
      return;
    }
    
    let html = '';
    for (const r of routers) {
      try {
        const rep = await apiFetch(`/api/routers/${r.id}/compliance`);
        const g = rep.grade || 'N/A';
        html += `<tr>
          <td><div class="router-name">${escapeHtml(r.name)}</div></td>
          <td><span class="grade-badge grade-${g}">${g}</span></td>
          <td>${rep.score} / 100</td>
          <td style="color:var(--accent-green)">${rep.passed ? rep.passed.length : 0} passed</td>
          <td style="color:var(--accent-red)">${rep.failed ? rep.failed.length : 0} failed</td>
          <td><button class="btn btn-sm" onclick="viewComplianceReport('${r.id}', '${escapeHtml(r.name)}')">View Report</button></td>
        </tr>`;
      } catch (e) {
        html += `<tr><td colspan="6">Failed to evaluate ${escapeHtml(r.name)}</td></tr>`;
      }
    }
    tbody.innerHTML = html;
  } catch (e) {
    showToast('Failed to load compliance data: ' + e.message, 'error');
  }
}

async function viewComplianceReport(routerId, routerName) {
  openModal(`🛡️ Security Report — ${routerName}`, '<div class="skeleton" style="height:300px"></div>');
  try {
    const rep = await apiFetch(`/api/routers/${routerId}/compliance`);
    
    let html = `<div style="display:flex; justify-content:space-between; margin-bottom: 20px;">
      <div><h2 style="margin-bottom:5px;">Security Score: ${rep.score} / 100</h2></div>
      <div><span class="grade-badge grade-${rep.grade}" style="width: 40px; height: 40px; font-size: 20px;">${rep.grade}</span></div>
    </div>`;
    
    if(rep.failed && rep.failed.length > 0) {
      html += `<h4 style="margin-top: 15px; margin-bottom: 10px;">🔥 Failed Checks</h4><ul style="color:var(--accent-red); margin-bottom: 20px; line-height: 1.6; list-style:none;">`;
      rep.failed.forEach(f => {
        html += `<li style="padding: 6px; background: rgba(255,51,102,.05); border-left: 3px solid var(--accent-red); margin-bottom: 8px;">
          <strong>[${escapeHtml(f.id)}]</strong> ${escapeHtml(f.description)}
        </li>`;
      });
      html += `</ul>`;
    } else {
      html += `<div style="padding: 10px; background: rgba(0,255,136,.05); border-left: 3px solid var(--accent-green); color:var(--accent-green); margin-bottom: 20px;">All checks passed!</div>`;
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
    console.warn("Auth check failed, assuming local dev or no auth", e);
  }
  
  document.getElementById('login-overlay').style.display = 'none';
  document.getElementById('app-layout').style.display = 'flex';
  refreshDashboard();
  pollTimer = setInterval(refreshDashboard, POLL_INTERVAL);
});
