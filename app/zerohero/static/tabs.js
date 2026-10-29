// Tabbladen op verdiepingspagina's: <nav class="tabs" data-tabs> met <a data-tab="x">, panelen met data-paneel="x".
// Het gekozen tabblad staat in de #-hash, zodat verversen, terugknop en periodeknoppen op hetzelfde tabblad blijven.
(function () {
  const nav = document.querySelector('nav.tabs[data-tabs]');
  if (!nav) return;
  const tabs = nav.querySelectorAll('[data-tab]');
  const panelen = document.querySelectorAll('[data-paneel]');
  const eerste = tabs.length ? tabs[0].dataset.tab : '';
  const naamVan = h => { h = (h || '').replace('#', ''); return [...panelen].some(p => p.dataset.paneel === h) ? h : eerste; };
  function toon(h, scroll) {
    const naam = naamVan(h);
    panelen.forEach(p => p.hidden = p.dataset.paneel !== naam);
    tabs.forEach(t => { const aan = t.dataset.tab === naam; t.classList.toggle('aan', aan); t.setAttribute('aria-selected', aan); });
    document.querySelectorAll('.periodes a, .sorteer a').forEach(a => { a.hash = naam === eerste ? '' : naam; });
    if (scroll) window.scrollTo({ top: nav.getBoundingClientRect().top + window.scrollY - 80 });
  }
  tabs.forEach(t => t.addEventListener('click', e => { e.preventDefault(); history.replaceState(null, '', '#' + t.dataset.tab); toon('#' + t.dataset.tab, false); }));
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href^="#"]');
    if (a && !a.dataset.tab && naamVan(a.getAttribute('href')) === a.getAttribute('href').slice(1)) {
      e.preventDefault(); history.replaceState(null, '', a.getAttribute('href')); toon(a.getAttribute('href'), true);
    }
  });
  window.addEventListener('hashchange', () => toon(location.hash));
  toon(location.hash);
})();
