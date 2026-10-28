// Methodology page: long bilingual prose, for two exams. The text lives in
// blocks per language ([data-lang-block="pl"|"en"]) and, where the exams differ,
// per exam ([data-exam-block="e8"|"matura"]); one element may carry both. Here
// we show the ones for the current UI language and exam, and keep the nav/title
// labels translated (setLang → applyI18N).

(function () {
  function showBlocks() {
    for (const el of document.querySelectorAll('[data-lang-block], [data-exam-block]')) {
      const lang = el.getAttribute('data-lang-block');
      const exam = el.getAttribute('data-exam-block');
      el.hidden = (!!lang && lang !== currentLang) || (!!exam && exam !== EXAM);
    }
  }
  setLang(resolvePref('lang', ['pl', 'en']));
  showBlocks();
  // The nav carries the exam, so the map and ranking links return to the exam
  // this page was opened from.
  rewriteExamLinks('.topnav nav a');
  const renderExamToggle = wireExamToggle();
  wireLangToggle(() => { showBlocks(); renderExamToggle(); });

  // Floating "back to contents" button: appears once you scroll down, and jumps
  // to the top (where the ToC lives). Avoids the downsides of a sticky ToC bar.
  const tocFab = document.getElementById('toc-fab');
  if (tocFab) {
    const toggleFab = () => tocFab.classList.toggle('visible', window.scrollY > 300);
    toggleFab();
    window.addEventListener('scroll', toggleFab, { passive: true });
    tocFab.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
  }
}());
