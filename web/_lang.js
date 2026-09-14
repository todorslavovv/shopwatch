// ---- language ----------------------------------------------------------
const FLAG_BG = '<svg viewBox="0 0 5 3" aria-hidden="true">' +
  '<rect width="5" height="3" fill="#fff"/><rect y="1" width="5" height="1" fill="#00966E"/>' +
  '<rect y="2" width="5" height="1" fill="#D62612"/></svg>';
const FLAG_EN = '<svg viewBox="0 0 60 30" aria-hidden="true">' +
  '<rect width="60" height="30" fill="#012169"/>' +
  '<path d="M0 0 60 30M60 0 0 30" stroke="#fff" stroke-width="6"/>' +
  '<path d="M0 0 60 30M60 0 0 30" stroke="#C8102E" stroke-width="2.5"/>' +
  '<path d="M30 0V30M0 15H60" stroke="#fff" stroke-width="10"/>' +
  '<path d="M30 0V30M0 15H60" stroke="#C8102E" stroke-width="6"/></svg>';

// Bulgarian first: these tools are for a Bulgarian unit, so BG is the default and
// English is the alternate, not the other way round.
let LANG = 'bg';
try { const s = localStorage.getItem('lang'); if (s === 'bg' || s === 'en') LANG = s; } catch (e) {}

const t = k => (I18N[LANG] && I18N[LANG][k] !== undefined) ? I18N[LANG][k] : I18N.bg[k];

function langSwitcher() {
  return `<div class="langsw" role="group" aria-label="${t('lang_aria')}">
    <button class="lang" data-lang="bg" aria-pressed="${LANG === 'bg'}" lang="bg">${FLAG_BG} БГ</button>
    <button class="lang" data-lang="en" aria-pressed="${LANG === 'en'}" lang="en">${FLAG_EN} EN</button>
  </div>`;
}

function bindLang() {
  document.querySelectorAll('.lang').forEach(b => b.addEventListener('click', () => {
    LANG = b.dataset.lang;
    try { localStorage.setItem('lang', LANG); } catch (e) {}
    document.documentElement.lang = LANG;
    render();
  }));
}
