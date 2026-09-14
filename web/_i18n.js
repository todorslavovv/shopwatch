const I18N = {
  bg: {
    lang_aria: 'Избор на език',
    title: 'Триаж на търговци',
    tag: 'Shopwatch · идентификация на търговеца',
    h1: 'Триаж на търговци',
    sub: 'Български онлайн магазини, подредени според това доколко публикуваната от тях ' +
      'идентификация на търговеца изостава от изискванията на закона. Всички проверки се ' +
      'извършват офлайн върху самата страница — без справка в регистър и без лични данни.',
    f_checked: 'проверени магазина', f_non: 'без съответствие',
    f_suspect: 'съмнителни', f_ok: 'в съответствие',
    worklist: 'Работен списък',
    worknote: 'най-проблемните първо · оценката е евристика, не вероятност',
    v_noncompliant: 'без съответствие', v_suspect: 'съмнителен', v_ok: 'в съответствие',
    fields: { eik: 'ЕИК', company_name: 'Фирма', address: 'Адрес',
              phone: 'Телефон', email: 'Имейл', vat_number: 'ДДС' },
    notpub: 'не е публикуван', nofind: 'без констатации',
    msg: { NO_EIK: 'На сайта не е публикуван ЕИК.',
           EIK_INVALID: 'Публикуваният ЕИК не преминава контролната сума.' },
    judged: 'Как се оценява магазин',
    checked_h: 'Проверява се офлайн', notclaimed_h: 'Умишлено не се твърди',
    checked: ['Дали изобщо е публикуван ЕИК — българското право го изисква от онлайн търговец.',
      'Дали публикуваният ЕИК преминава контролната си сума по модул 11.',
      'Дали са посочени адрес на управление и начин за връзка.',
      'Етикетите се разчитат на български и английски, включително стойности, ' +
      'разделени от маркирането на страницата.'],
    notclaimed: ['<strong>Валидна контролна сума не означава, че дружеството съществува.</strong> ' +
      'Това изисква Търговския регистър, който тук не се заявява.',
      'Само число от девет цифри никога не се приема за ЕИК — иначе цени, номера на поръчки ' +
      'и телефони също биха се броили.',
      'Нищо не идентифицира лице. Управители и собственици са извън обхвата по замисъл.',
      'Липсващо оповестяване е нарушение на закона, а не доказателство за измама.'],
    prov: 'Оценено офлайн само по маркирането на страницата — без заявки към регистър, без ' +
      'мрежови заявки, без събиране на лични данни. Оценката тежи най-силно липсващ или ' +
      'невалиден идентификатор; тежестите са документирани в кода и представляват евристика ' +
      'за приоритизиране, а не вероятност за измама.',
  },
  en: {
    lang_aria: 'Language',
    title: 'Merchant Disclosure Triage',
    tag: 'Shopwatch · merchant disclosure',
    h1: 'Merchant Disclosure Triage',
    sub: 'Bulgarian online shops ranked by how far their published trader identity falls short ' +
      'of what the law requires. Every check runs offline against the page itself — no register ' +
      'lookup, no personal data.',
    f_checked: 'shops checked', f_non: 'non-compliant', f_suspect: 'suspect', f_ok: 'compliant',
    worklist: 'Triage worklist',
    worknote: 'worst first · score is a heuristic, not a probability',
    v_noncompliant: 'noncompliant', v_suspect: 'suspect', v_ok: 'ok',
    fields: { eik: 'ЕИК', company_name: 'Company', address: 'Address',
              phone: 'Phone', email: 'Email', vat_number: 'VAT' },
    notpub: 'not published', nofind: 'no findings',
    msg: { NO_EIK: 'No ЕИК/UIC identifier published on the shop site.',
           EIK_INVALID: 'The published ЕИК fails its checksum.' },
    judged: 'How a shop is judged',
    checked_h: 'Checked offline', notclaimed_h: 'Deliberately not claimed',
    checked: ['Whether an ЕИК is published at all — Bulgarian law requires it of an online trader.',
      'Whether a published ЕИК passes its mod-11 checksum.',
      'Whether a registered address and a means of contact are given.',
      'Labels are read in Bulgarian and English, including values split across page markup.'],
    notclaimed: ['<strong>A valid checksum does not mean the company exists.</strong> That needs ' +
      'the Commercial Register, which is not queried here.',
      'A bare nine-digit number is never read as an ЕИК — prices, order numbers and phone ' +
      'numbers would all qualify.',
      'Nothing identifies a person. Directors and owners are out of scope by design.',
      'A missing disclosure is a legal shortfall, not proof of fraud.'],
    prov: 'Assessed offline from page markup alone — no register queries, no network calls, no ' +
      'personal data collected. Scores weight a missing or invalid identifier most heavily; the ' +
      'weighting is documented in the source and is a triage heuristic, not a probability of fraud.',
  },
};
