const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ORDER = { noncompliant: 0, suspect: 1, ok: 2 };
const rows = [...DATA].sort((a, b) =>
  ORDER[a.assessment.verdict] - ORDER[b.assessment.verdict] ||
  b.assessment.score - a.assessment.score);

const tone = v => v === 'noncompliant' ? 'bad' : v === 'suspect' ? 'warn' : 'good';
const FIELD_ORDER = ['eik', 'company_name', 'address', 'phone', 'email', 'vat_number'];
const MONO = new Set(['eik', 'phone', 'email', 'vat_number']);

const shopHtml = r => {
  const a = r.assessment, m = r.merchant, v = a.verdict;
  const crit = new Set(a.findings.filter(f => /^(NO_EIK|EIK_INVALID)$/.test(f.code)).map(f => f.code));
  const critCode = a.findings.find(f => crit.has(f.code));
  // Prefer the translated message; fall back to whatever the assessor emitted.
  const critMsg = critCode ? (t('msg')[critCode.code] || critCode.message) : null;
  const snips = Object.entries(r.evidence || {}).slice(0, 2)
    .map(([k, s]) => `${k}: ${String(s).replace(/\s+/g, ' ').trim().slice(0, 110)}`);

  return `<article class="shop v-${esc(v)}">
    <div class="shop-main">
      <div class="shop-head">
        <span class="shop-dom">${esc(r.domain)}</span>
        <span class="vchip v-${esc(v)}">${esc(t('v_' + v) || v)}</span>
        <span class="score ${tone(v)}">${a.score}</span>
      </div>
      ${critMsg ? `<div style="margin-top:7px;font-size:13px;color:var(--seal)">${esc(critMsg)}</div>` : ''}
      <div class="findings">${a.findings.map(f =>
        `<span class="f${crit.has(f.code) ? ' critical' : ''}">${esc(f.code)}</span>`).join('')
        || `<span class="f">${esc(t('nofind'))}</span>`}</div>
      ${snips.length ? `<div class="snip">${snips.map(esc).join('<br>')}</div>` : ''}
    </div>
    <div class="shop-side">
      <dl class="fields">${FIELD_ORDER.map(k => {
        const val = m[k], label = t('fields')[k];
        return `<dt>${esc(label)}</dt><dd class="${val ? (MONO.has(k) ? 'mono' : '') : 'miss'}">${
          val ? esc(val) : esc(t('notpub'))}</dd>`;
      }).join('')}</dl>
    </div>
  </article>`;
};

function render() {
  document.documentElement.lang = LANG;
  document.title = t('title');
  document.getElementById('langslot').innerHTML = langSwitcher();
  bindLang();
  document.getElementById('tag').textContent = t('tag');
  document.getElementById('h1').textContent = t('h1');
  document.getElementById('sub').textContent = t('sub');

  const count = v => rows.filter(r => r.assessment.verdict === v).length;
  document.getElementById('facts').innerHTML = [
    [t('f_checked'), rows.length, ''], [t('f_non'), count('noncompliant'), 'bad'],
    [t('f_suspect'), count('suspect'), 'warn'], [t('f_ok'), count('ok'), 'good'],
  ].map(([k, v, c]) => `<div class="fact"><div class="k">${esc(k)}</div>
      <div class="v ${c}">${v}</div></div>`).join('');

  document.getElementById('app').innerHTML = `
    <section class="panel">
      <header><h2>${esc(t('worklist'))}</h2><span class="note">${esc(t('worknote'))}</span></header>
      ${rows.map(shopHtml).join('')}
    </section>
    <section class="panel">
      <header><h2>${esc(t('judged'))}</h2></header>
      <div class="proof">
        <div><h3 class="yes">${esc(t('checked_h'))}</h3>
          <ul>${t('checked').map(i => `<li>${i}</li>`).join('')}</ul></div>
        <div><h3 class="no">${esc(t('notclaimed_h'))}</h3>
          <ul>${t('notclaimed').map(i => `<li>${i}</li>`).join('')}</ul></div>
      </div>
    </section>`;

  document.getElementById('prov').textContent = t('prov');
}
render();
