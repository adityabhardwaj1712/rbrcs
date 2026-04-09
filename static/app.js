/**
 * RBRCS — Frontend JavaScript
 * Handles API calls, toast notifications, and interactive features
 */

// ── Toast Notification System ─────────────────────────────

function showToast(message, type = 'info') {
    let container = document.querySelector('.toast-container');
    if (!container) {
        container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.remove();
        if (container.children.length === 0) {
            container.remove();
        }
    }, 4000);
}

// ── API Helpers ───────────────────────────────────────────

async function apiCall(url, method = 'POST', body = null) {
    try {
        const options = {
            method,
            headers: { 'Content-Type': 'application/json' },
        };
        if (body) options.body = JSON.stringify(body);

        const res = await fetch(url, options);
        const data = await res.json();
        return data;
    } catch (err) {
        showToast(`Request failed: ${err.message}`, 'error');
        throw err;
    }
}

// ── Backup Functions ──────────────────────────────────────

async function backupRouter(routerId) {
    const btn = document.getElementById(`btn-backup-${routerId}`) ||
                document.getElementById('btn-backup-now');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Backing up…';
    }

    try {
        const result = await apiCall(`/api/backup/${routerId}`);
        if (result.success) {
            if (result.is_new) {
                showToast(`✅ New backup saved! (ID: ${result.config_id})`, 'success');
            } else {
                showToast('ℹ️ Config unchanged — no backup needed', 'info');
            }
        } else {
            showToast(`⚠️ Backup issue: ${result.message}`, 'warning');
        }
    } catch (err) {
        showToast('❌ Backup failed', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = btn.id === 'btn-backup-now'
                ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Backup Now'
                : 'Backup Now';
        }
    }
}

async function backupAll() {
    const btn = document.getElementById('btn-backup-all');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Backing up all…';
    }

    try {
        const result = await apiCall('/api/backup-all');
        const newCount = result.results.filter(r => r.is_new).length;
        const failCount = result.results.filter(r => !r.success).length;

        if (failCount > 0) {
            showToast(`⚠️ ${newCount} new backups, ${failCount} failed`, 'warning');
        } else if (newCount > 0) {
            showToast(`✅ ${newCount} new backup(s) saved`, 'success');
        } else {
            showToast('ℹ️ All configs unchanged', 'info');
        }

        // Reload page to show updated data
        setTimeout(() => location.reload(), 1500);
    } catch (err) {
        showToast('❌ Backup all failed', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Backup All';
        }
    }
}

// ── Restore Functions ─────────────────────────────────────

async function restoreLatest(routerId) {
    if (!confirm('⚠️ Restore the latest backup to this router?\n\nThis will overwrite the current running configuration.')) {
        return;
    }

    const btn = document.getElementById('btn-restore-latest');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Restoring…';
    }

    try {
        const result = await apiCall(`/api/restore/${routerId}`);
        if (result.success) {
            showToast(`✅ ${result.message}`, 'success');
        } else {
            showToast(`❌ ${result.message}`, 'error');
        }
    } catch (err) {
        showToast('❌ Restore failed', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg> Restore Latest';
        }
    }
}

async function restoreConfig(routerId, configId) {
    if (!confirm(`⚠️ Restore config #${configId} to this router?\n\nThis will overwrite the current running configuration.`)) {
        return;
    }

    try {
        const result = await apiCall(`/api/restore/${routerId}`, 'POST', { config_id: configId });
        if (result.success) {
            showToast(`✅ ${result.message}`, 'success');
        } else {
            showToast(`❌ ${result.message}`, 'error');
        }
    } catch (err) {
        showToast('❌ Restore failed', 'error');
    }
}

// ── Auto-refresh Stats ────────────────────────────────────

function startAutoRefresh(intervalMs = 60000) {
    setInterval(async () => {
        try {
            const stats = await apiCall('/api/stats', 'GET');
            // Update stat values if elements exist
            const statEl = document.querySelector('#stat-routers .stat-value');
            if (statEl) statEl.textContent = stats.total_routers;

            const backupEl = document.querySelector('#stat-backups .stat-value');
            if (backupEl) backupEl.textContent = stats.total_backups;

            const storageEl = document.querySelector('#stat-storage .stat-value');
            if (storageEl) storageEl.textContent = stats.total_storage_formatted;
        } catch {
            // Silent fail on auto-refresh
        }
    }, intervalMs);
}

// ── Initialize ────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    // Start auto-refresh on dashboard
    if (document.getElementById('stat-routers')) {
        startAutoRefresh(60000); // Every 60 seconds
    }
    
    // UI logic for router additions
    const devType = document.getElementById('router-type');
    if(devType) {
        devType.addEventListener('change', (e) => {
            const pwdGroup = document.getElementById('enable-pwd-group');
            if (e.target.value === 'cisco_ios') {
                pwdGroup.style.display = 'flex';
            } else {
                pwdGroup.style.display = 'none';
            }
        });
    }
});

// ── Submit logic for Add Router ─────────────────────────────
async function submitRouterForm(e) {
    e.preventDefault();
    const btn = document.getElementById('btn-submit-router');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Saving...';

    const rawFormData = new FormData(e.target);
    const data = Object.fromEntries(rawFormData.entries());

    try {
        const result = await apiCall('/api/routers', 'POST', data);
        if (result.success) {
            showToast('✅ ' + result.message, 'success');
            setTimeout(() => {
                window.location.href = `/router/${data.id}`;
            }, 1000);
        } else {
            showToast('❌ ' + result.message, 'error');
            btn.disabled = false;
            btn.innerHTML = originalText;
        }
    } catch (err) {
        console.error(err);
        btn.innerHTML = originalText;
    }
}

// ── Deploy Ad-hoc Config ────────────────────────────────
async function deployAdhocConfig(routerId) {
    const payloadInput = document.getElementById('config-payload');
    const commands = payloadInput.value;
    if (!commands.trim()) return showToast('Please enter some configuration lines first', 'warning');

    const btn = document.getElementById('btn-deploy-config');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Deploying...';

    const outputContainer = document.getElementById('deploy-output-container');
    const outputBlock = document.getElementById('deploy-output');
    
    // Hide old output
    outputContainer.style.display = 'none';
    outputBlock.textContent = '';

    try {
        const result = await apiCall(`/api/router/${routerId}/configure`, 'POST', { commands });
        outputContainer.style.display = 'block';
        
        if (result.success) {
            showToast('✅ Deployment complete', 'success');
            outputBlock.textContent = result.output;
            payloadInput.value = ''; // clear upon success optionally
        } else {
            showToast('❌ Deployment failed: ' + result.message, 'error');
            outputBlock.textContent = result.output ? result.output : result.message;
        }
    } catch (err) {
        console.error(err);
        showToast('❌ System Error', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalText;
    }
}

// ── Client-Side Quick Filters ───────────────────────────
function filterRouters() {
    const searchInput = document.getElementById('router-search');
    if(!searchInput) return;
    
    const filterText = searchInput.value.toLowerCase();
    const rows = document.querySelectorAll('.router-matrix-row');
    
    rows.forEach(row => {
        const searchText = row.getAttribute('data-search') || '';
        if (searchText.includes(filterText)) {
            row.style.display = '';
        } else {
            row.style.display = 'none';
        }
    });
}
