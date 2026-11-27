const range = document.querySelector('#range');
const errorBox = document.querySelector('#error');
const alertsBox = document.querySelector('#alerts');
const refresh = document.querySelector('#refresh');

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
  if (!items.length) { alertsBox.innerHTML = '<p class="empty">No match alerts in this date range.</p>'; return; }
  alertsBox.innerHTML = items.map(item => `<article class="alert-row">
    <div><p class="primary-text">${escapeHtml(item.recipient)}</p><p class="secondary">with ${escapeHtml(item.other_agent)}</p></div>
    <div class="area"><p>${escapeHtml(item.area)}</p><span class="pill">Sent</span></div>
    <div class="score"><p class="primary-text">${item.score == null ? '—' : escapeHtml(item.score) + '%'}</p><p class="secondary">Match score</p></div>
    <p class="secondary">${escapeHtml(formatDate(item.sent_at))}</p>
    <div class="links">${item.lead_url ? `<a href="${escapeHtml(item.lead_url)}" target="_blank" rel="noopener">Lead</a>` : ''}${item.listing_url ? `<a href="${escapeHtml(item.listing_url)}" target="_blank" rel="noopener">Listing</a>` : ''}</div>
  </article>`).join('');
}
async function load() {
  refresh.disabled = true; errorBox.classList.add('hidden');
  try {
    const query = encodeURIComponent(range.value);
    const [stats, alerts] = await Promise.all([getJson(`/api/stats?range=${query}`), getJson(`/api/alerts?range=${query}`)]);
    document.querySelectorAll('[data-stat]').forEach(node => { node.textContent = Number(stats[node.dataset.stat]).toLocaleString('en-MY'); });
    renderAlerts(alerts.alerts);
    document.querySelector('#updated').textContent = `Updated ${new Intl.DateTimeFormat('en-MY',{timeStyle:'short',timeZone:'Asia/Kuala_Lumpur'}).format(new Date())}`;
  } catch (error) { errorBox.textContent = error.message; errorBox.classList.remove('hidden'); }
  finally { refresh.disabled = false; }
}
range.addEventListener('change', load); refresh.addEventListener('click', load); load();
