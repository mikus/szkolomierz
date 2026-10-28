// Methodology page: long bilingual prose. The text lives as two blocks in the
// HTML ([data-lang-block="pl"|"en"]); here we show the one for the current UI
// language and keep the nav/title labels translated (setLang → applyI18N).

(function () {
  function showLangContent() {
    for (const el of document.querySelectorAll('[data-lang-block]')) {
      el.hidden = el.getAttribute('data-lang-block') !== currentLang;
    }
  }
  setLang(resolvePref('lang', ['pl', 'en']));
  showLangContent();
  // The nav carries the exam, so the map and ranking links return to the exam
  // this page was opened from.
  rewriteExamLinks('.topnav nav a');
  const renderExamToggle = wireExamToggle();
  wireLangToggle(() => { showLangContent(); renderExamToggle(); });

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
