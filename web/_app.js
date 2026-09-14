const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ORDER = { noncompliant: 0, suspect: 1, ok: 2 };
const rows = [...DATA].sort((a, b) =>
  ORDER[a.assessment.verdict] - ORDER[b.assessment.verdict] ||
  b.assessment.score - a.assessment.score);

const tone = v => v === 'noncompliant' ? 'bad' : v === 'suspect' ? 'warn' : 'good';

// ---- summary -----------------------------------------------------------
const count = v => rows.filter(r => r.assessment.verdict === v).length;
document.getElementById('facts').innerHTML = [
  ['shops checked', rows.length, ''],
  ['non-compliant', count('noncompliant'), 'bad'],
  ['suspect', count('suspect'), 'warn'],
  ['compliant', count('ok'), 'good'],
].map(([k, v, c]) => `<div class="fact"><div class="k">${k}</div>
    <div class="v ${c}">${v}</div></div>`).join('');

// ---- worklist ----------------------------------------------------------
const FIELDS = [
  ['eik', 'ЕИК', true], ['company_name', 'Company', false], ['address', 'Address', false],
  ['phone', 'Phone', true], ['email', 'Email', true], ['vat_number', 'ДДС', true],
];

const shopHtml = r => {
  const a = r.assessment, m = r.merchant, v = a.verdict;
  const crit = new Set(a.findings.filter(f =>
    /^(NO_EIK|EIK_INVALID)$/.test(f.code)).map(f => f.code));
  const critMsg = a.findings.find(f => crit.has(f.code));
  const snips = Object.entries(r.evidence || {}).slice(0, 2)
    .map(([k, s]) => `${k}: ${String(s).replace(/\s+/g, ' ').trim().slice(0, 110)}`);

  return `<article class="shop v-${esc(v)}">
    <div class="shop-main">
      <div class="shop-head">
        <span class="shop-dom">${esc(r.domain)}</span>
        <span class="vchip v-${esc(v)}">${esc(v)}</span>
        <span class="score ${tone(v)}">${a.score}</span>
      </div>
      ${critMsg ? `<div style="margin-top:7px;font-size:13px;color:var(--seal)">
          ${esc(critMsg.message)}</div>` : ''}
      <div class="findings">${a.findings.map(f =>
        `<span class="f${crit.has(f.code) ? ' critical' : ''}">${esc(f.code)}</span>`).join('')
        || '<span class="f">no findings</span>'}</div>
      ${snips.length ? `<div class="snip">${snips.map(esc).join('<br>')}</div>` : ''}
    </div>
    <div class="shop-side">
      <dl class="fields">${FIELDS.map(([k, label, mono]) => {
        const val = m[k];
        return `<dt>${esc(label)}</dt><dd class="${val ? (mono ? 'mono' : '') : 'miss'}">${
          val ? esc(val) : 'not published'}</dd>`;
      }).join('')}</dl>
    </div>
  </article>`;
};

// ---- page --------------------------------------------------------------
document.getElementById('app').innerHTML = `
  <section class="panel">
    <header><h2>Triage worklist</h2>
      <span class="note">worst first &middot; score is a heuristic, not a probability</span></header>
    ${rows.map(shopHtml).join('')}
  </section>

  <section class="panel">
    <header><h2>How a shop is judged</h2></header>
    <div class="proof">
      <div><h3 class="yes">Checked offline</h3><ul>
        <li>Whether an ЕИК is published at all &mdash; Bulgarian law requires it of an online trader.</li>
        <li>Whether a published ЕИК passes its mod-11 checksum.</li>
        <li>Whether a registered address and a means of contact are given.</li>
        <li>Labels are read in Bulgarian and English, including values split across page markup.</li>
      </ul></div>
      <div><h3 class="no">Deliberately not claimed</h3><ul>
        <li><strong>A valid checksum does not mean the company exists.</strong> That needs the
            Commercial Register, which is not queried here.</li>
        <li>A bare nine-digit number is never read as an ЕИК &mdash; prices, order numbers and
            phone numbers would all qualify.</li>
        <li>Nothing identifies a person. Directors and owners are out of scope by design.</li>
        <li>A missing disclosure is a legal shortfall, not proof of fraud.</li>
      </ul></div>
    </div>
  </section>`;

document.getElementById('prov').innerHTML =
  `Assessed offline from page markup alone &mdash; no register queries, no network calls, ` +
  `no personal data collected. Scores weight a missing or invalid identifier most heavily; ` +
  `the weighting is documented in the source and is a triage heuristic, not a probability of fraud.`;
