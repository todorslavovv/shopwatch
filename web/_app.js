const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// The example worklist: real shops, hand-verified shortfalls, each with its evidence.
const EX = DATA || { shops: [], summary: {} };
const SEVERITY = ['EIK_OTHER_COMPANY', 'EIK_INVALID', 'NO_EIK'];
const rank = r => Math.min(...r.shortfalls.map(s => {
  const i = SEVERITY.indexOf(s.code); return i < 0 ? 9 : i; }));
const rows = [...EX.shops].sort((a, b) => rank(a) - rank(b) || b.shortfalls.length - a.shortfalls.length);

// Five shops per page, the page named by a real ?page=N query parameter. The build runs
// this same code in node to pre-render every page, and the server maps ?page=N to that
// file, so the links work without JavaScript too.
const PER_PAGE = 5;
function pageFromSearch(search, pages) {
  const m = /(?:^|[?&])page=([^&#]*)/.exec(search || '');
  const n = m && /^\d+$/.test(m[1]) ? parseInt(m[1], 10) : 1;   // "abc", "-1", "2.5": page 1
  return Math.min(Math.max(n, 1), pages);                         // above the last: the last
}
const pageHref = n => `?page=${n}#findings`;
function pagerHtml(page, pages, total) {
  const first = (page - 1) * PER_PAGE + 1, last = Math.min(page * PER_PAGE, total);
  const edge = (n, label, rel) => n >= 1 && n <= pages
    ? `<a class="pg-step" href="${pageHref(n)}" rel="${rel}">${esc(label)}</a>`
    : `<span class="pg-step" aria-disabled="true">${esc(label)}</span>`;
  const nums = Array.from({ length: pages }, (_, i) => i + 1).map(n => n === page
    ? `<a class="pg-num active" href="${pageHref(n)}" aria-current="page" aria-label="${esc(t('pg_page')(n))}">${n}</a>`
    : `<a class="pg-num" href="${pageHref(n)}" aria-label="${esc(t('pg_page')(n))}">${n}</a>`).join('');
  return `<nav class="pager" aria-label="${esc(t('pg_aria'))}">
    <p class="pg-range">${esc(t('pg_showing')(first, last, total))}</p>
    <div class="pg-links">${edge(page - 1, '← ' + t('pg_prev'), 'prev')}${nums}${edge(page + 1, t('pg_next') + ' →', 'next')}</div>
  </nav>`;
}

const tone = v => v === 'noncompliant' ? 'bad' : v === 'suspect' ? 'warn' : 'good';
const FIELD_ORDER = ['eik', 'company_name', 'address', 'phone', 'email', 'vat_number'];
const EX_FIELDS = ['eik', 'legal_name', 'legal_form', 'address', 'phone', 'email', 'vat_number'];
const MONO = new Set(['eik', 'phone', 'email', 'vat_number']);

const evHtml = e => `<li><span class="tier ${e.source_type === 'vies' || e.source_type === 'nap_register' ? 'official' : 'company_source'}">${
    esc(t('stype')[e.source_type] || e.source_type)}</span>
  <span class="why"> ${esc(t('checked_on'))} ${esc(String(e.retrieved_at || '').slice(0, 10))}</span>
  <div class="ev-url">${esc(e.url)}</div>
  ${e.excerpt ? `<div class="snip">${esc(e.excerpt)}</div>` : ''}</li>`;

const shopHtml = r => {
  const n = r.shortfalls.filter(s => s.kind !== 'information').length;
  const info = r.shortfalls.length - n;
  const crit = r.shortfalls.some(s => SEVERITY.includes(s.code));
  return `<article class="shop ${crit ? 'v-noncompliant' : 'v-suspect'}">
    <div class="shop-main">
      <div class="shop-head">
        <span class="shop-dom">${esc(r.domain)}</span>
        <span class="vchip ${crit ? 'v-noncompliant' : 'v-suspect'}">${esc(t('sf_n')(n))}</span>
        ${info ? `<span class="vchip v-info">+ ${info} ${esc(t('k_information'))}</span>` : ''}
        <span class="lc-conf">${esc(t('checked_on'))} ${esc(r.checked_at)}</span>
      </div>
      <ul class="sflist">${r.shortfalls.map(s => `<li class="${s.kind === 'information' ? 'info' : ''}">
        <span class="kind">${esc(t(s.kind === 'information' ? 'k_information' : 'k_shortfall'))}</span>
        <strong>${esc(t('sf')[s.code] || s.code)}</strong> — ${esc(LANG === 'bg' ? s.bg : s.en)}
        ${s.auto == null ? '' : `<div class="auto ${s.auto ? 'yes' : 'no'}">${s.auto ? '✓ ' : ''}${esc(s.auto ? t('auto_yes') : t('auto_no'))}</div>`}
        <details class="srcs"><summary>${esc(t('evidence'))} (${s.evidence.length})</summary>
          <ul>${s.evidence.map(evHtml).join('')}</ul></details></li>`).join('')}</ul>
    </div>
    <div class="shop-side">
      <dl class="fields">${EX_FIELDS.map(k => {
        const f = r.fields[k] || {};
        const v = f.value != null ? [].concat(f.value).join('; ') : f.published ? t('pubd') : null;
        return `<dt>${esc(t('fields')[k])}</dt><dd class="${f.published ? (MONO.has(k) && f.value ? 'mono' : '') : 'miss'}">${
          esc(v || t('notpub'))}</dd>`;
      }).join('')}</dl>
      <div class="why" style="margin-top:10px">${esc(t('declared'))}: <span class="mono">${esc(r.declared_eik.join(', '))}</span>${
        r.vies_name ? ` · ${esc(t('vies_name'))}: ${esc(r.vies_name)}` : ` · ${esc(t('not_in_vies'))}`}</div>
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
  document.getElementById('checkh').textContent = t('check_h');
  document.getElementById('checkp').textContent = t('check_p');

  const pages = Math.max(1, Math.ceil(rows.length / PER_PAGE));
  const page = pageFromSearch(location.search, pages);
  const sm = EX.summary || {};
  const facts = [
    [t('f_shops'), sm.shops, ''], [t('f_sf'), sm.shortfalls, 'bad'], [t('f_info'), sm.information, ''],
    [t('f_auto'), sm.findings ? `${sm.detected_automatically}/${sm.findings}` : '—', ''],
    [t('f_date'), EX.generated || '—', ''],
  ].map(([k, v, c]) => `<div class="fact"><div class="k">${esc(k)}</div>
      <div class="v ${c}">${esc(v)}</div></div>`).join('')
    + (sm.findings ? `<p class="bench">${esc(t('bench')(sm.detected_automatically, sm.findings))}</p>` : '');

  const block = (h, items) => `<div><h3>${esc(t(h))}</h3>${t(items).map(p => `<p>${p}</p>`).join('')}</div>`;
  document.getElementById('app').innerHTML = `
    <section class="panel">
      <header><h2>${esc(t('about'))}</h2></header>
      <div class="whygrid">${block('what_h', 'what')}${block('mean_h', 'mean')}${block('access_h', 'access')}${block('scale_h', 'scale')}</div>
    </section>
    <section class="panel" id="findings">
      <header><h2>${esc(t('worklist'))}</h2></header>
      <div class="whynote">${esc(t('worknote'))}<div class="facts">${facts}</div></div>
      ${rows.slice((page - 1) * PER_PAGE, page * PER_PAGE).map(shopHtml).join('')}
      ${pagerHtml(page, pages, rows.length)}
    </section>`;

  document.getElementById('prov').textContent = t('prov');
  document.getElementById('liveurl').placeholder = t('lc_placeholder');
  document.getElementById('liveurl').setAttribute('aria-label', t('lc_aria'));
  renderLive();
}

// ---- live domain check: results render here, the page never navigates ------
// Everything in a result came from a hostile site or a third party: escape it all,
// and show URLs as text, never as links, so nobody clicks through to a scam page.
let LIVE = null;          // {state: 'loading'|'done'|'error', data, message}

const tierOf = (c, basis) => !c ? 'unverified'
  : c.verified_by === 'registry' ? 'registry' : c.verified_by === 'official' ? 'vies'
  : (basis || 'company_source');
const tierChip = k => `<span class="tier ${esc(k)}">${esc(t('tier')[k] || k)}</span>`;
const stTone = (s, o) => s === 'available' && o !== 'no_evidence_found' ? 'good'
  : ['blocked', 'failed'].includes(s) ? 'bad' : 'meh';
const evType = s => s.type === 'official' && /VIES/.test(s.name || '') ? 'vies'
  : s.type === 'official' && /Registry/.test(s.name || '') ? 'registry' : s.type;
const tsDate = ts => ts ? `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}` : '';

// ---- compliance profile: one row per field, its own status and sources ---------
const PF_ORDER = ['legal_name', 'eik', 'legal_form', 'registered_address', 'phone', 'email',
                  'vat_number', 'vat_status', 'domain_association'];
const tsDay = ts => ts ? `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}` : '';
const srcName = s => (t('psrc')[s.type] || s.label) + (s.archived ? ` (${t('lc_archived_short')} ${tsDay(s.archived)})` : '');

function fieldValue(k, f, d) {
  if (f.value == null) return '';
  if (k === 'vat_status') return t('pvat')[f.value] || f.value;
  if (k === 'domain_association') {
    const who = (d.profile.fields.legal_name || {}).value || ((d.company || {}).name) || '';
    return `${f.value} → ${who}`;
  }
  return f.value;
}

function fieldBadge(f) {
  const kinds = [...new Set((f.sources || []).map(s => s.type))];
  const best = kinds.includes('registry') ? 'registry' : kinds.includes('vies') ? 'vies' : kinds[0];
  const lbl = best ? (t('psrc')[best] || best) : '';
  if (f.status === 'verified') return `<span class="fs ok">✓ ${esc(lbl)}</span>`;
  if (f.status === 'corroborated') return `<span class="fs ok">✓ ${esc(kinds.map(k => t('psrc')[k] || k).join(' + '))}</span>`;
  if (f.status === 'published') return `<span class="fs pub">${esc(t('pfs').published)}${
    kinds.includes('company_archive') ? ' · ' + esc(t('lc_archived_short')) : ''}</span>`;
  if (f.status === 'conflicting') return `<span class="fs bad">⚠ ${esc(t('pfs').conflicting)}</span>`;
  if (f.status === 'ai_assisted') return `<span class="fs pub">ⓘ ${esc(t('pai'))}</span>`;
  if (f.status === 'ai_assisted_unretrieved') return `<span class="fs bad">⚠ ${esc(t('pai_unret'))}</span>`;
  if (f.status === 'not_independently_verified') return `<span class="fs na">${esc(t('pnv'))}</span>`;
  if (f.status === 'unavailable') return `<span class="fs na">${esc(t('pfr')[f.reason] || t('pfs').unavailable)}</span>`;
  return `<span class="fs na">${esc(t('pfs').not_found)}</span>`;
}

function fieldSources(k, f) {
  const groups = (f.values && f.values.length ? f.values : [{ value: f.value, sources: f.sources }])
    .filter(g => g.sources && g.sources.length);
  const note = f.reason === 'checked' && f.params ? t('pfr_checked')(f.params)
    : f.reason && t('pfr')[f.reason] && f.status !== 'unavailable' ? t('pfr')[f.reason] : f.note;
  if (!groups.length) return note ? `<div class="why">${esc(note)}</div>` : '';
  const many = groups.length > 1;
  return `<details class="srcs"><summary>${esc(t('psources'))} (${groups.reduce((n, g) => n + g.sources.length, 0)})</summary>
    ${many && f.status === 'conflicting' ? `<div class="lc-conflict" style="margin:6px 0">⚠ ${esc(t('pconflict'))}</div>` : ''}
    ${groups.map(g => `<div class="srcgroup">${many ? `<div><strong>${esc(fieldValue(k, { value: g.value }, { profile: { fields: {} } }))}</strong></div>` : ''}
      <ul>${g.sources.map(s => `<li>${s.discovery_method === 'ai_assisted' ? `<div class="why">${esc(s.retrieved === false ? '⚠ ' + t('pai_unret') : 'ⓘ ' + t('pai'))} ${esc(t('pai_src'))}:</div>` : ''}<span class="tier ${esc(s.type === 'registry' || s.type === 'vies' ? 'official' : 'company_source')}">${esc(srcName(s))}</span>
        ${s.retrieved_at ? `<span class="why"> ${esc(t('lc_retrieved'))} ${esc(String(s.retrieved_at).slice(0, 10))}</span>` : ''}
        ${s.url ? `<div class="ev-url">${esc(s.url)}</div>` : ''}
        ${s.raw && String(s.raw) !== String(g.value) ? `<div class="why">${esc(t('praw'))}: ${esc(String(s.raw).slice(0, 240))}</div>` : ''}</li>`).join('')}</ul></div>`).join('')}
    ${note ? `<div class="why">${esc(note)}</div>` : ''}</details>`;
}

function complianceHtml(d) {
  const p = d.profile;
  if (!p) return '';
  const sm = p.summary;
  const counts = ['verified', 'corroborated', 'published', 'conflicting', 'not_found', 'unavailable']
    .filter(k => sm[k]).map(k => `${sm[k]} ${esc(t('pfs_n')[k])}`).join(' · ');
  const rows = PF_ORDER.filter(k => p.fields[k]).map(k => {
    const f = p.fields[k];
    const more = f.values && f.values.length > 1 && f.status !== 'conflicting'
      ? `<div class="why">${esc(t('pmore'))}: ${f.values.slice(1, 6).map(v => esc(v.value)).join(', ')}</div>` : '';
    return `<tr><th>${esc(t('pf')[k])}</th>
      <td class="${['eik', 'vat_number', 'phone', 'email'].includes(k) ? 'mono' : ''}">${
        f.value == null ? '<span class="why">—</span>' : esc(fieldValue(k, f, d))}${more}</td>
      <td>${fieldBadge(f)}${fieldSources(k, f)}</td></tr>`;
  }).join('');
  return `<section class="panel comp">
    <header><h2>${esc(t('pc_title'))}</h2><span class="note">${esc(t('pc_note'))}</span></header>
    <div class="lc-note" style="background:none">
      <strong>${esc(t('pc_identity'))}:</strong> ${esc(t('pid')[p.identity_status] || p.identity_status)}
      &nbsp;·&nbsp; ${sm.found}/${sm.fields} ${esc(t('pc_found'))} &nbsp;·&nbsp; ${counts}</div>
    <div class="tblwrap"><table class="ptable"><thead><tr><th>${esc(t('pc_field'))}</th><th>${
      esc(t('pc_value'))}</th><th>${esc(t('pc_verif'))}</th></tr></thead><tbody>${rows}</tbody></table></div>
  </section>`;
}

function liveHtml(d) {
  const r = d.domain_resolution, c = d.company, tier = tierOf(c, r.basis);
  const head = `<div class="lc-head">
      <span class="lc-dom">${esc(d.canonical_domain)}</span>
      <span class="vchip ${r.company_identified ? 'v-ok' : 'v-suspect'}">${
        esc(r.company_identified ? t('lc_resolved') : t('lc_unresolved'))}</span>
      <span class="lc-conf">${esc(t('lc_conf'))}: <strong>${esc(t('conf_lvl')[r.confidence_level] || r.confidence_level)}</strong>${
        d.cached ? ' · ' + esc(t('lc_cached')) : ''}</span>
    </div>
    <div class="lc-note" style="background:none"><strong>${esc(t('lc_reasons'))}:</strong>
      <ul class="actions" style="margin-top:4px">${(r.reason_codes || []).map(x => {
        const f = t('rs')[x.code];   // English is the server's own text
        return `<li>${esc(f ? f(x.params || {}) : x.text)}</li>`; }).join('')}</ul>
      ${r.candidate ? `<div style="margin-top:6px">${tierChip('unverified')} ${esc(t('lc_candidate'))}: <span class="mono">${esc(r.candidate.eik)}</span></div>` : ''}
    </div>`;
  const conflicts = (d.conflicts || []).map(x => `<p class="lc-conflict">⚠ ${esc(t('cfl')[x.type] || x.type)}${
      x.official_name ? ': ' + esc(x.official_name) : ''}${
      x.eiks ? ': ' + x.eiks.map(e => esc(e.eik)).join(', ') : ''}${
      x.fields ? ': ' + x.fields.map(esc).join(', ') : ''}</p>`).join('');
  const order = ['eik', 'name', 'legal_form', 'status', 'registered_address', 'registration_date', 'capital', 'vat_number'];
  const pf = d.profile && d.profile.fields;
  const company = c && pf ? `<div>${tierChip(tier)}</div><dl class="fields pfields" style="margin-top:10px">${
      PF_ORDER.filter(k => k !== 'vat_status' && pf[k]).map(k => { const f = pf[k];
        const v = k === 'vat_number' && f.value && pf.vat_status && pf.vat_status.value
          ? `${f.value} · ${t('pvat')[pf.vat_status.value] || ''}` : fieldValue(k, f, d);
        return `<dt>${esc(t('pf')[k])}</dt><dd><span class="${['eik', 'vat_number', 'phone', 'email'].includes(k) ? 'mono' : ''}">${
          v ? esc(v) : '—'}</span> ${fieldBadge(f)}</dd>`; }).join('')}</dl>`
    : c ? `<div>${tierChip(tier)}</div><dl class="fields" style="margin-top:10px">${
      order.filter(k => c[k]).map(k => `<dt>${esc(t('cf')[k] || k)}</dt><dd class="${
        k === 'eik' || k === 'vat_number' ? 'mono' : ''}">${
        esc(k === 'status' ? (t('cstatus')[c[k]] || c[k]) : c[k])}</dd>`).join('')}</dl>`
    : `<div>${tierChip('unverified')}</div><p style="font-size:13px;color:var(--ink-soft)">${esc(t('lc_none'))}</p>`;
  const people = (d.people || []).length
    ? `<dl class="fields">${d.people.map(p => `<dt>${esc(t('prole')[p.role] || p.role || '')}</dt><dd>${esc(p.name)}
        <div class="why">${esc(t('pst')[p.status] || '')}${p.source && !p.source_url ? ' · ' + esc(p.source) : ''}</div>
        ${p.source_url ? `<div class="ev-url">${esc(p.source_url)}</div>` : ''}
        <div class="ev-url">${esc(t('lc_retrieved'))} ${esc(String(p.retrieved_at || p.last_verified || '—').slice(0, 10))}</div></dd>`).join('')}</dl>`
    : `<p style="font-size:12.5px;color:var(--ink-faint);margin:0">${esc(t('lc_people_off'))}</p>`;
  const evidence = (d.sources || []).length ? `<ul class="ev-list">${d.sources.map(s => `<li>${tierChip(evType(s))}
      ${s.claim_code ? ' ' + esc((t('claim')[s.claim_code] || s.claim_code) + (s.value ? ': ' + s.value : ''))
        : s.name ? ' ' + esc(s.name) : ''}
      ${s.url ? `<div class="ev-url">${s.archived ? esc(t('lc_archived')) + ' ' : ''}${esc(s.url)}${
        s.archived ? ' · ' + esc(tsDate(s.archived)) : ''}</div>` : ''}
      ${s.snippet ? `<div class="snip" style="margin-top:4px">${esc(s.snippet)}</div>` : ''}
      <div class="ev-url">${esc(t('lc_retrieved'))} ${esc(s.retrieved_at || '—')}</div></li>`).join('')}</ul>`
    : `<p style="font-size:13px;color:var(--ink-faint);margin:0">—</p>`;
  const checks = `<dl class="chk">${Object.entries(d.discovery || {}).map(([k, v]) =>
      `<dt>${esc(t('chk')[k] || k)}${v.supporting_only ? ' *' : ''}</dt><dd class="${stTone(v.status, v.outcome)}">${
        esc(t('st')[v.status] || v.status || '—')}${v.outcome ? ' · ' + esc(t('oc')[v.outcome] || v.outcome) : ''}</dd>`).join('')}</dl>
      <p style="font-size:11.5px;color:var(--ink-faint);margin:8px 0 0">* ${esc(t('lc_infra_note'))}</p>`;
  const disc = d.disclosure;
  const discHtml = disc ? `<div class="shop-head"><span class="vchip v-${esc(disc.assessment.verdict)}">${
        esc(t('v_' + disc.assessment.verdict) || disc.assessment.verdict)}</span>
      <span class="score ${tone(disc.assessment.verdict)}">${disc.assessment.score ?? '—'}</span></div>
      <dl class="fields" style="margin-top:8px">${FIELD_ORDER.map(k => {
        const val = disc.merchant[k];
        return `<dt>${esc(t('fields')[k])}</dt><dd class="${val ? (MONO.has(k) ? 'mono' : '') : 'miss'}">${
          val ? esc(val) : esc(t('notpub'))}</dd>`; }).join('')}</dl>
      <div class="findings">${disc.assessment.findings.map(f => `<span class="f">${esc(f.code)}</span>`).join('')}</div>`
    : `<p style="font-size:13px;color:var(--ink-soft);margin:0">${esc(t('lc_target'))}: ${
        esc(t('st')[d.target_site.fetch_status] || d.target_site.fetch_status)}</p>`;
  const inf = d.infrastructure || {};
  const infra = `<details class="lc"><summary>${esc(t('lc_infra'))}</summary>
      <p style="font-size:12.5px;color:var(--ink-soft)">${esc(t('lc_infra_note'))}</p>
      <pre class="lc-infra">${esc(JSON.stringify({ dns: inf.dns, rdap: inf.rdap, tls: inf.tls,
        ct_hosts: inf.ct_hosts, pages_read: d.target_site.pages_read }, null, 1))}</pre></details>`;
  return `<section class="panel">
    <header><h2>${esc(t('lc_title'))}</h2><span class="note">${esc(d.checked_at)}</span></header>
    ${head}${conflicts}
    <div class="lc-grid">
      <div><h3>${esc(t('lc_company'))}</h3>${company}
        <h3 style="margin-top:14px">${esc(t('lc_people'))}</h3>${people}</div>
      <div><h3>${esc(t('lc_evidence'))}</h3>${evidence}</div>
      <div><h3>${esc(t('lc_sources'))}</h3>${checks}</div>
      <div><h3>${esc(t('lc_disclosure'))}</h3>${discHtml}</div>
    </div>
    <div class="lc-note">${infra}</div>
    <div class="lc-note">${esc(t('lc_disclaimer'))}</div>
  </section>${complianceHtml(d)}`;
}

// ---- progress while a lookup runs ---------------------------------------------
// The server runs the check as a job and reports the stage it is really in; this page
// only draws what it is told. Nothing here advances on a timer.
const STAGE_IDS = ['fetching', 'discovering', 'legal_sources', 'verifying', 'result'];
const MARK = { done: '✓', skipped: '–', pending: '○', failed: '✕', cancelled: '✕' };

function progressHtml(L) {
  const j = L.job || { stages: STAGE_IDS.map(id => ({ id, status: 'pending' })), message: { code: 'queued' } };
  const msg = t('jm')[j.message && j.message.code] || '';
  const steps = j.stages.map(s => `<li class="step ${esc(s.status)}">
      ${s.status === 'active' ? '<span class="spin" aria-hidden="true"></span>'
        : `<span class="mark" aria-hidden="true">${MARK[s.status] || '○'}</span>`}
      <span>${esc(t('stage')[s.id] || s.id)}</span>${
      s.status === 'skipped' ? `<span class="why">${esc(t('stage_skipped'))}</span>` : ''}</li>`).join('');
  const p = j.provisional;
  const prov = p && (p.eik || p.name) ? `<div class="prov">
      ${tierChip('provisional')} <strong>${esc(t('prov_title'))}</strong>
      <dl class="fields" style="margin-top:8px">
        ${p.name ? `<dt>${esc(t('cf').name)}</dt><dd>${esc(p.name)}</dd>` : ''}
        ${p.eik ? `<dt>${esc(t('cf').eik)}</dt><dd class="mono">${esc(p.eik)}</dd>` : ''}
        ${p.host ? `<dt>${esc(t('prov_source'))}</dt><dd class="mono">${esc(p.host)}${
          p.archived ? ' · ' + esc(t('lc_archived')) + ' ' + esc(p.archived) : ''}</dd>` : ''}
      </dl>
      <div class="why" style="margin-top:6px">${esc(t('prov_note'))}</div></div>` : '';
  return `<section class="panel" aria-busy="true">
    <header><h2>${esc(t('lc_title'))}</h2><span class="note lc-elapsed"></span></header>
    <div class="lc-head"><span class="lc-dom">${esc(L.domain || '')}</span></div>
    <ol class="steps">${steps}</ol>
    <div class="lc-note" role="status">${esc(msg)}</div>
    ${prov}
    <div class="lc-actions"><button type="button" class="btn-ghost" data-act="cancel">${
      esc(t('btn_cancel'))}</button></div>
  </section>`;
}

function renderLive() {
  const el = document.getElementById('live');
  const input = document.getElementById('liveurl'), btn = document.getElementById('livebtn');
  const busy = !!(LIVE && LIVE.state === 'running');
  btn.disabled = busy; input.readOnly = busy;
  btn.textContent = busy ? t('lc_busy') : t('lc_button');
  if (!LIVE) { el.innerHTML = ''; el.dataset.key = ''; return; }
  if (LIVE.state === 'running') {
    // Redraw only when the progress itself changed: a panel rebuilt on every poll would
    // swallow a click on Cancel that happens to straddle the rebuild.
    const j = LIVE.job || {};
    const key = JSON.stringify([LANG, LIVE.domain, j.stages, j.message, j.provisional]);
    if (el.dataset.key !== key || !el.querySelector('.steps')) {
      el.innerHTML = progressHtml(LIVE);
      el.dataset.key = key;
    }
    const secs = el.querySelector('.lc-elapsed');
    if (secs) secs.textContent = LIVE.started ? Math.round((Date.now() - LIVE.started) / 1000) + ' s' : '';
    return;
  }
  el.dataset.key = '';
  if (LIVE.state === 'error') {
    el.innerHTML = `<section class="panel"><div class="lc-msg err" role="alert">${esc(LIVE.message)}</div>
      <div class="lc-actions"><button type="button" class="btn-ghost" data-act="new">${
        esc(t('btn_new'))}</button></div></section>`;
  } else {
    el.innerHTML = liveHtml(LIVE.data);
  }
}

const API = '/api/domain-check';     // absolute: the page is under /shopwatch/
const sleep = ms => new Promise(r => setTimeout(r, ms));
const errText = (code, fallback) => t('jerr')[code] || fallback || t('jerr').internal;

async function runLive(url) {
  const token = {};                  // a newer lookup or a cancel retires this loop
  LIVE = { state: 'running', token, started: Date.now(),
           domain: url.replace(/^https?:\/\//, '').replace(/\/.*$/, '') };
  renderLive();
  let res, body = null;
  try {
    res = await fetch(API, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                             body: JSON.stringify({ url, async: true }) });
    try { body = await res.json(); } catch (e) { /* proxy error page, not JSON */ }
  } catch (e) {
    LIVE = { state: 'error', message: t('lc_offline') }; renderLive(); return;
  }
  if (!res.ok || !body || !body.job_id) {
    LIVE = { state: 'error', message: errText(body && body.error, body && body.message) || `HTTP ${res.status}` };
    renderLive(); return;
  }
  LIVE = { ...LIVE, job: body, domain: body.domain || LIVE.domain };
  const started = Date.now();
  let misses = 0;
  while (LIVE && LIVE.token === token) {
    const j = LIVE.job;
    if (j.state === 'completed') { LIVE = { state: 'done', data: j.result }; break; }
    if (j.state === 'failed') { LIVE = { state: 'error', message: errText(j.error && j.error.code, j.error && j.error.message) }; break; }
    if (j.state === 'cancelled') { LIVE = null; break; }
    renderLive();
    const age = Date.now() - started;
    if (age > 200000) { LIVE = { state: 'error', message: t('jerr').timeout }; break; }
    await sleep(age < 10000 ? 700 : age < 30000 ? 1000 : 1500);   // gentle backoff
    if (!LIVE || LIVE.token !== token) return;
    try {
      const r = await fetch(`${API}/${encodeURIComponent(j.job_id)}`, { cache: 'no-store' });
      const b = await r.json().catch(() => null);
      if (r.status === 404) { LIVE = { state: 'error', message: t('jerr').job_not_found }; break; }
      if (!r.ok || !b) throw new Error(`HTTP ${r.status}`);
      misses = 0;
      if (LIVE && LIVE.token === token) LIVE = { ...LIVE, job: b };
    } catch (e) {
      if (++misses >= 4) { LIVE = { state: 'error', message: t('jerr').network }; break; }
    }
  }
  renderLive();
}

document.getElementById('live').addEventListener('click', async ev => {
  const act = ev.target.closest('[data-act]');
  if (!act) return;
  if (act.dataset.act === 'cancel' && LIVE && LIVE.job) {
    const id = LIVE.job.job_id;
    LIVE = null; renderLive();                              // the page is free at once
    try { await fetch(`${API}/${encodeURIComponent(id)}/cancel`, { method: 'POST' }); } catch (e) {}
  } else if (act.dataset.act === 'new' || act.dataset.act === 'cancel') {
    LIVE = null; renderLive();
  }
  document.getElementById('liveurl').focus();
});

document.getElementById('livecheck').addEventListener('submit', ev => {
  ev.preventDefault();
  const url = document.getElementById('liveurl').value.trim();
  if (url && !(LIVE && LIVE.state === 'running')) runLive(url);   // no double submits
});

render();
