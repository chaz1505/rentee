const range = document.querySelector('#range');
const errorBox = document.querySelector('#error');
const alertsBox = document.querySelector('#alerts');
const refresh = document.querySelector('#refresh');
const filters = [...document.querySelectorAll('[data-status]')];
let statusFilter = 'all';

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}
function formatDate(value) {
  if (!value) return 'Time unavailable';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Time unavailable';
  return new Intl.DateTimeFormat('en-MY', {dateStyle:'medium',timeStyle:'short',timeZone:'Asia/Kuala_Lumpur'}).format(date);
}
async function getJson(path) {
  const response = await fetch(path, {credentials:'same-origin',headers:{Accept:'application/json'}});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Dashboard data is temporarily unavailable.');
  return data;
}
function renderAlerts(items) {
  if (!items.length) { alertsBox.innerHTML = '<p class="empty">No matches in this date range.</p>'; return; }
  alertsBox.innerHTML = items.map(item => `<article class="alert-row">
    <div class="match-heading"><p class="primary-text">${escapeHtml(item.lead_owner)} — ${escapeHtml(item.listing_owner)}</p><p class="secondary">${escapeHtml(formatDate(item.created_at))}</p></div>
    <div class="match-summary"><p>${escapeHtml(item.area)} · <span class="match-score ${item.score != null && Number(item.score) >= Number(item.threshold) ? 'qualifying' : ''}">${item.score == null ? 'Not scored' : escapeHtml(item.score) + '/100'}</span></p>${item.explanation ? `<p class="secondary explanation">${escapeHtml(item.explanation)}</p>` : ''}</div>
    <div class="alert-state"><span class="pill ${item.status === 'Sent' ? 'sent' : 'not-sent'}">${escapeHtml(item.status)}</span><p class="secondary">Recipient: ${escapeHtml(item.alert_recipient)}</p>${item.unsent_reason ? `<p class="secondary">Reason: ${escapeHtml(item.unsent_reason)}</p>` : ''}</div>
    <div class="links">${item.lead_url ? `<a href="${escapeHtml(item.lead_url)}" target="_blank" rel="noopener">View Lead</a>` : ''}${item.listing_url ? `<a href="${escapeHtml(item.listing_url)}" target="_blank" rel="noopener">View Listing</a>` : ''}</div>
  </article>`).join('');
}
async function load() {
  refresh.disabled = true; errorBox.classList.add('hidden');
  try {
    const query = encodeURIComponent(range.value);
    const [stats, alerts] = await Promise.all([getJson(`/api/stats?range=${query}`), getJson(`/api/matches?range=${query}&status=${encodeURIComponent(statusFilter)}`)]);
    document.querySelectorAll('[data-stat]').forEach(node => { node.textContent = Number(stats[node.dataset.stat]).toLocaleString('en-MY'); });
    alerts.matches.forEach(item => { item.threshold = alerts.threshold; });
    renderAlerts(alerts.matches);
    document.querySelector('#updated').textContent = `Updated ${new Intl.DateTimeFormat('en-MY',{timeStyle:'short',timeZone:'Asia/Kuala_Lumpur'}).format(new Date())}`;
  } catch (error) { errorBox.textContent = error.message; errorBox.classList.remove('hidden'); }
  finally { refresh.disabled = false; }
}
range.addEventListener('change', load); refresh.addEventListener('click', load); load();
filters.forEach(button => button.addEventListener('click', () => {
  statusFilter = button.dataset.status;
  filters.forEach(item => item.classList.toggle('active', item === button));
  load();
}));
