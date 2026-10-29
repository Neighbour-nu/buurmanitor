// Sorteerbare tabellen: <table class="sorteerbaar"> met <th data-sorteer>. Cellen met data-waarde (getal of datum JJJJ-MM-DD).
// Eerste klik: hoog naar laag, tweede klik: laag naar hoog. Lege waarden staan altijd onderaan.
(function () {
  document.querySelectorAll('table.sorteerbaar').forEach(tabel => {
    const koppen = tabel.querySelectorAll('thead th');
    const body = tabel.tBodies[0];
    koppen.forEach((th, i) => {
      if (!th.hasAttribute('data-sorteer')) return;
      th.tabIndex = 0;
      th.setAttribute('role', 'button');
      th.title = 'Klik om te sorteren';
      const sorteer = () => {
        const af = th.dataset.richting !== 'af';
        koppen.forEach(k => { delete k.dataset.richting; k.removeAttribute('aria-sort'); });
        th.dataset.richting = af ? 'af' : 'op';
        th.setAttribute('aria-sort', af ? 'descending' : 'ascending');
        const waarde = tr => {
          const td = tr.children[i];
          const v = td ? (td.dataset.waarde ?? td.textContent.trim()) : '';
          if (v === '') return null;
          const n = Number(v);
          return Number.isNaN(n) ? v : n;
        };
        const rijen = [...body.rows];
        rijen.sort((a, b) => {
          const x = waarde(a), y = waarde(b);
          if (x === null && y === null) return 0;
          if (x === null) return 1;
          if (y === null) return -1;
          const c = x < y ? -1 : x > y ? 1 : 0;
          return af ? -c : c;
        });
        rijen.forEach(r => body.appendChild(r));
      };
      th.addEventListener('click', sorteer);
      th.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sorteer(); } });
    });
  });
})();
