const range = document.querySelector('#range');
const errorBox = document.querySelector('#error');
const alertsBox = document.querySelector('#alerts');
const pagination = document.querySelector('#pagination');
const matchCount = document.querySelector('#match-count');
const recentHeading = document.querySelector('#recent-heading');
const refresh = document.querySelector('#refresh');
const filters = [...document.querySelectorAll('[data-status]')];
let statusFilter = 'all';
let currentPage = 1;
let requestSequence = 0;
let loading = false;

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
function pageItems(page, total) {
  if (total <= 7) return Array.from({length: total}, (_, index) => index + 1);
  const values = new Set([1, total, page - 1, page, page + 1]);
  const pages = [...values].filter(value => value >= 1 && value <= total).sort((a, b) => a - b);
  const output = [];
  pages.forEach((value, index) => {
    if (index && value - pages[index - 1] > 1) output.push('…');
    output.push(value);
  });
  return output;
}
function renderPagination(data) {
  const start = data.total_matches ? (data.page - 1) * data.page_size + 1 : 0;
  const end = Math.min(data.page * data.page_size, data.total_matches);
  matchCount.textContent = data.total_matches && start <= end ? `Showing ${start}–${end} of ${data.total_matches.toLocaleString('en-MY')} matches` : data.total_matches ? `Showing 0 of ${data.total_matches.toLocaleString('en-MY')} matches` : 'Showing 0 matches';
  const numbers = pageItems(data.page, data.total_pages).map(value => value === '…'
    ? '<span class="page-ellipsis" aria-hidden="true">…</span>'
    : `<button type="button" class="page-button${value === data.page ? ' active' : ''}" data-page="${value}" ${value === data.page ? 'aria-current="page"' : ''}>${value}</button>`).join('');
  pagination.innerHTML = `<button type="button" class="page-button page-direction" data-page="${data.page - 1}" ${data.has_previous ? '' : 'disabled'}>Previous</button><div class="page-numbers">${numbers}</div><button type="button" class="page-button page-direction" data-page="${data.page + 1}" ${data.has_next ? '' : 'disabled'}>Next</button>`;
}
async function load({includeStats = true, scrollToMatches = false} = {}) {
  loading = true;
  const sequence = ++requestSequence;
  refresh.disabled = true; errorBox.classList.add('hidden');
  alertsBox.setAttribute('aria-busy', 'true');
  alertsBox.innerHTML = '<p class="empty">Loading matches…</p>';
  try {
    const query = encodeURIComponent(range.value);
    const statsRequest = includeStats ? getJson(`/api/stats?range=${query}`) : Promise.resolve(null);
    const [stats, alerts] = await Promise.all([statsRequest, getJson(`/api/matches?range=${query}&status=${encodeURIComponent(statusFilter)}&page=${currentPage}`)]);
    if (sequence !== requestSequence) return;
    if (stats) document.querySelectorAll('[data-stat]').forEach(node => { node.textContent = Number(stats[node.dataset.stat]).toLocaleString('en-MY'); });
    alerts.matches.forEach(item => { item.threshold = alerts.threshold; });
    renderAlerts(alerts.matches);
    renderPagination(alerts);
    document.querySelector('#updated').textContent = `Updated ${new Intl.DateTimeFormat('en-MY',{timeStyle:'short',timeZone:'Asia/Kuala_Lumpur'}).format(new Date())}`;
    if (scrollToMatches) recentHeading.scrollIntoView({behavior:'smooth', block:'start'});
  } catch (error) { errorBox.textContent = error.message; errorBox.classList.remove('hidden'); }
  finally { if (sequence === requestSequence) { loading = false; refresh.disabled = false; alertsBox.removeAttribute('aria-busy'); } }
}
range.addEventListener('change', () => { currentPage = 1; load(); });
refresh.addEventListener('click', () => load());
pagination.addEventListener('click', event => {
  const button = event.target.closest('[data-page]');
  if (!button || button.disabled || loading) return;
  currentPage = Number(button.dataset.page);
  load({includeStats:false, scrollToMatches:true});
});
load();
filters.forEach(button => button.addEventListener('click', () => {
  statusFilter = button.dataset.status;
  currentPage = 1;
  filters.forEach(item => item.classList.toggle('active', item === button));
  load();
}));
