// A dashboard lapozó-logikájának izolált tesztje (DOM stub-okkal).
import fs from 'fs';

const html = fs.readFileSync(new URL('../templates/hu/dashboard.html', import.meta.url), 'utf8');
const grab = (name) => {
  const i = html.indexOf(`function ${name}(`);
  if (i < 0) throw new Error('not found: ' + name);
  let depth = 0, j = html.indexOf('{', i);
  for (let k = j; k < html.length; k++) {
    if (html[k] === '{') depth++;
    else if (html[k] === '}') { depth--; if (!depth) return html.slice(i, k + 1); }
  }
};

const els = {
  'page-number': { textContent: '1' },
  'prev-btn': { disabled: false },
  'next-btn': { disabled: false },
  'assembly-per-page': { value: '25' },
  'assembly-filter-state': { textContent: '', className: '' },
};
globalThis.document = { getElementById: (id) => els[id] || null };

let assemblyPage = 1, assemblyTotal = null, assemblyTotalPages = null,
    assemblyHasMore = false, assemblyRowCount = 0, assemblyFilterDate = null;
let loaded = 0;
const loadAssemblyTable = () => { loaded++; };
const src = grab('updateAssemblyPager') + '\n' + grab('changeAssemblyPage')
          + '\n' + grab('updateAssemblyFilterState')
          + '\nexport {updateAssemblyPager, changeAssemblyPage, updateAssemblyFilterState};';
const mod = await import('data:text/javascript;base64,' + Buffer.from(
  src.replace(/\bassemblyPage\b/g, 'G.assemblyPage')
     .replace(/\bassemblyTotalPages\b/g, 'G.assemblyTotalPages')
     .replace(/\bassemblyTotal\b(?!Pages)/g, 'G.assemblyTotal')
     .replace(/\bassemblyHasMore\b/g, 'G.assemblyHasMore')
     .replace(/\bassemblyRowCount\b/g, 'G.assemblyRowCount')
     .replace(/\bassemblyFilterDate\b/g, 'G.assemblyFilterDate')
     .replace(/\bloadAssemblyTable\(\)/g, 'G.loadAssemblyTable()')
).toString('base64'));

globalThis.G = { assemblyPage, assemblyTotal, assemblyTotalPages, assemblyHasMore,
                 assemblyRowCount, assemblyFilterDate, loadAssemblyTable };

const eq = (got, want, what) => {
  const ok = String(got) === String(want);
  console.log(`  ${ok ? '✓' : '✗'} ${what}: ${got}${ok ? '' : `   (várt: ${want})`}`);
  if (!ok) process.exitCode = 1;
};

console.log('── napra szűrve: 27 sor, 25/oldal ────────────');
G.assemblyTotal = 27; G.assemblyTotalPages = 2; G.assemblyPage = 1;
G.assemblyHasMore = true; G.assemblyRowCount = 25;
mod.updateAssemblyPager();
eq(els['page-number'].textContent, '1. / 2 oldal · 27 sor', 'felirat');
eq(els['prev-btn'].disabled, true,  'Előző tiltva az 1. oldalon');
eq(els['next-btn'].disabled, false, 'Következő aktív');

mod.changeAssemblyPage(1);
eq(G.assemblyPage, 2, 'lapozás előre');

G.assemblyPage = 2; G.assemblyHasMore = false; G.assemblyRowCount = 2;
mod.updateAssemblyPager();
eq(els['page-number'].textContent, '2. / 2 oldal · 27 sor', 'utolsó oldal felirata');
eq(els['next-btn'].disabled, true, 'Következő tiltva az utolsó oldalon');

const before = loaded;
mod.changeAssemblyPage(1);
eq(G.assemblyPage, 2, 'nem lép túl az utolsó oldalon');
eq(loaded, before, 'nem tölt újra feleslegesen');

console.log('── date=all: ismeretlen összdarabszám ────────────');
G.assemblyTotal = null; G.assemblyTotalPages = null;
G.assemblyPage = 1; G.assemblyHasMore = true; G.assemblyRowCount = 25;
mod.updateAssemblyPager();
eq(els['page-number'].textContent, '1. oldal · 25 sor', 'felirat összdarabszám nélkül');
eq(els['next-btn'].disabled, false, 'Következő aktív, mert has_more');

G.assemblyPage = 2; G.assemblyHasMore = false; G.assemblyRowCount = 7;
mod.updateAssemblyPager();
eq(els['page-number'].textContent, '2. oldal · 7 sor', 'utolsó oldal összdarabszám nélkül');
eq(els['next-btn'].disabled, true, 'Következő tiltva, nincs több');

console.log('── üres eredmény ─────────────────────────────────');
G.assemblyTotal = 0; G.assemblyTotalPages = 1; G.assemblyPage = 1;
G.assemblyHasMore = false; G.assemblyRowCount = 0;
mod.updateAssemblyPager();
eq(els['page-number'].textContent, 'nincs találat', 'üres felirat');

console.log('── szűrő badge ───────────────────────────────────');
G.assemblyFilterDate = '2026-09-16'; mod.updateAssemblyFilterState();
eq(els['assembly-filter-state'].textContent, 'Szűrő: 2026-09-16', 'napra szűrve');
G.assemblyFilterDate = null; mod.updateAssemblyFilterState();
eq(els['assembly-filter-state'].textContent, 'Szűrő: minden nap', 'szűrés törölve');

console.log(process.exitCode ? '\nFAIL' : '\nMINDEN TESZT OK');
