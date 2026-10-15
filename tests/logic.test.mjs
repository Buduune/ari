// Tests for the pure logic inside web/index.html (between LOGIC-START and LOGIC-END).
// Run: node --test tests/logic.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const block = html.split('/* LOGIC-START */')[1]?.split('/* LOGIC-END */')[0];
assert.ok(block, 'LOGIC-START / LOGIC-END markers are missing in web/index.html');
const L = new Function(`${block}; return { canPlace, countSolutions, fullGrid, makePuzzle, seattleOffsetHours, sunsetLocal, goldenStart, fmtTime };`)();

const isValidGrid = g => g.length === 16 && g.every((v, i) => {
  if (v < 1 || v > 4) return false;
  const copy = g.slice(); copy[i] = 0;
  return L.canPlace(copy, i, v);
});

test('a full grid follows the rules', () => {
  for (let k = 0; k < 200; k++) assert.ok(isValidGrid(L.fullGrid()));
});

test('every puzzle is easy (8 clues), matches its solution and has exactly one answer', () => {
  for (let k = 0; k < 500; k++) {
    const { sol, puzzle } = L.makePuzzle(8);
    assert.ok(isValidGrid(sol));
    assert.equal(puzzle.filter(Boolean).length, 8);
    puzzle.forEach((v, i) => { if (v) assert.equal(v, sol[i]); });
    assert.equal(L.countSolutions(puzzle, 3), 1);
  }
});

test('an empty grid has many answers', () => {
  assert.equal(L.countSolutions(new Array(16).fill(0), 2), 2);
});

test('Seattle clock offset follows daylight saving time', () => {
  assert.equal(L.seattleOffsetHours(2026, 2, 7), -8);   // Mar 7, 2026: before DST
  assert.equal(L.seattleOffsetHours(2026, 2, 8), -7);   // Mar 8: DST starts
  assert.equal(L.seattleOffsetHours(2026, 9, 31), -7);  // Oct 31: still DST
  assert.equal(L.seattleOffsetHours(2026, 10, 1), -8);  // Nov 1: DST ends
  assert.equal(L.seattleOffsetHours(2027, 2, 14), -7);  // Mar 14, 2027: DST starts
});

test('sunset matches reference times within 3 minutes', () => {
  // Reference: astral (NOAA algorithm) for Seattle, local clock time.
  const ref = {
    '2026-10-15': '18:20', '2026-10-24': '18:04', '2026-10-31': '17:52', '2026-11-01': '16:51',
    '2026-11-07': '16:42', '2026-11-26': '16:22', '2026-12-21': '16:20', '2027-01-10': '16:38',
    '2027-03-07': '18:02', '2027-03-14': '19:13', '2027-06-21': '21:10'
  };
  for (const [day, hhmm] of Object.entries(ref)) {
    const [y, m, d] = day.split('-').map(Number);
    const s = L.sunsetLocal(y, m - 1, d);
    const [rh, rm] = hhmm.split(':').map(Number);
    const diff = Math.abs((s.h * 60 + s.m) - (rh * 60 + rm));
    assert.ok(diff <= 3, `${day}: got ${s.h}:${s.m}, expected about ${hhmm}`);
  }
});

test('golden hour starts about an hour before sunset, on a quarter hour', () => {
  for (let d = 1; d <= 120; d++) {
    const t = new Date(Date.UTC(2026, 9, 15 + d));
    const g = L.goldenStart(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate());
    const start = g.h * 60 + g.m, sunset = g.sunset.h * 60 + g.sunset.m;
    assert.equal(g.m % 15, 0);
    assert.ok(sunset - start >= 52 && sunset - start <= 68, `${t.toISOString()}: ${start} vs ${sunset}`);
  }
});

test('golden hour in late October is 5pm; in December it is mid-afternoon', () => {
  assert.deepEqual([L.goldenStart(2026, 9, 24).h, L.goldenStart(2026, 9, 24).m], [17, 0]);
  assert.equal(L.goldenStart(2026, 11, 21).h, 15);
});

test('times read like the chips: 2pm, 5:15pm', () => {
  assert.equal(L.fmtTime(14, 0), '2pm');
  assert.equal(L.fmtTime(17, 15), '5:15pm');
  assert.equal(L.fmtTime(19, 0), '7pm');
  assert.equal(L.fmtTime(12, 0), '12pm');
  assert.equal(L.fmtTime(0, 30), '12:30am');
  assert.equal(L.fmtTime(9, 5), '9:05am');
});
