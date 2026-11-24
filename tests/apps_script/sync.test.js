/**
 * Node harness for apps_script/sync_to_supabase.gs.
 * Runs the real .gs file in a VM with in-memory fakes for SpreadsheetApp,
 * UrlFetchApp (a tiny PostgREST emulator), Utilities, etc.
 *   node tests/apps_script/sync.test.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');
const assert = require('assert');

// ---------- fake spreadsheet ----------
class FakeRange {
  constructor(sheet, r, c, nr, nc) { Object.assign(this, { sheet, r, c, nr, nc }); }
  getValues() {
    const out = [];
    for (let i = 0; i < this.nr; i++) {
      const row = this.sheet.data[this.r - 1 + i] || [];
      const vals = [];
      for (let j = 0; j < this.nc; j++) { const v = row[this.c - 1 + j]; vals.push(v === undefined ? '' : v); }
      out.push(vals);
    }
    return out;
  }
  setValues(vals) {
    for (let i = 0; i < this.nr; i++) {
      const ri = this.r - 1 + i;
      this.sheet.data[ri] = this.sheet.data[ri] || [];
      for (let j = 0; j < this.nc; j++) this.sheet.data[ri][this.c - 1 + j] = vals[i][j];
    }
    this.sheet.writes++;
    return this;
  }
  setValue(v) { return this.setValues([[v]]); }
  clearContent() { return this.setValues(Array.from({ length: this.nr }, () => Array(this.nc).fill(''))); }
  clearContents() { return this.clearContent(); }
  setFontWeight() { return this; }
}
class FakeSheet {
  constructor(name, data) { this.name = name; this.data = data; this.maxCols = 26; this.writes = 0; this.hidden = []; }
  getName() { return this.name; }
  getLastRow() { let n = 0; this.data.forEach((r, i) => { if (r && r.some(v => v !== '' && v !== undefined && v !== null)) n = i + 1; }); return n; }
  getLastColumn() { let n = 0; this.data.forEach(r => { (r || []).forEach((v, j) => { if (v !== '' && v !== undefined && v !== null) n = Math.max(n, j + 1); }); }); return n; }
  getMaxColumns() { return this.maxCols; }
  insertColumnsAfter(_, k) { this.maxCols += k; }
  hideColumns(c) { this.hidden.push(c); }
  getRange(r, c, nr = 1, nc = 1) { return new FakeRange(this, r, c, nr, nc); }
  clearContents() { this.data = []; }
  appendRow(row) { this.data[this.getLastRow()] = row.slice(); }
  deleteRows(start, k) { this.data.splice(start - 1, k); }
}
class FakeSpreadsheet {
  constructor() { this.sheets = {}; }
  getSheetByName(n) { return this.sheets[n] || null; }
  insertSheet(n) { return (this.sheets[n] = new FakeSheet(n, [])); }
  getSpreadsheetTimeZone() { return 'Asia/Colombo'; }
}

// ---------- fake PostgREST ----------
class FakeSupabase {
  constructor() { this.tx = new Map(); this.seen = []; this.calls = []; this.failNext = []; }
  handle(url, opts) {
    const u = new URL(url);
    const body = opts.payload ? JSON.parse(opts.payload) : null;
    this.calls.push({ method: opts.method, path: u.pathname, query: u.search, headers: opts.headers, body });
    if (this.failNext.length) { const code = this.failNext.shift(); return resp(code, '{"message":"injected"}'); }
    if (!opts.headers.apikey) return resp(401, 'no apikey');
    if (u.pathname === '/rest/v1/sales_transactions' && opts.method === 'post') {
      assert.strictEqual(u.searchParams.get('on_conflict'), 'row_id');
      assert.match(opts.headers.Prefer, /resolution=merge-duplicates/);
      const keys = JSON.stringify(Object.keys(body[0]).sort());
      for (const rec of body) {
        assert.strictEqual(JSON.stringify(Object.keys(rec).sort()), keys, 'PostgREST requires identical keys in a bulk insert');
        const prev = this.tx.get(rec.row_id);
        this.tx.set(rec.row_id, Object.assign({}, prev, rec));
      }
      return resp(201, '');
    }
    if (u.pathname === '/rest/v1/sales_transactions' && opts.method === 'get') return resp(200, '[]');
    if (u.pathname === '/rest/v1/sync_seen_rows') { this.seen.push(...body); return resp(201, ''); }
    if (u.pathname === '/rest/v1/rpc/finalize_sales_sync') {
      const ids = new Set(this.seen.filter(s => s.run_id === body.p_run_id).map(s => s.row_id));
      let deleted = 0;
      for (const [id, rec] of this.tx) if (!ids.has(id) && !rec.deleted_at) { rec.deleted_at = 'now'; deleted++; }
      return resp(200, JSON.stringify({ soft_deleted: deleted, seen: ids.size }));
    }
    return resp(404, 'not found ' + u.pathname);
  }
}
function resp(code, text) { return { getResponseCode: () => code, getContentText: () => text }; }

// ---------- sandbox ----------
function makeContext(ss, db, props) {
  const logs = [];
  const fmt = (d, tz) => new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
  const ctx = {
    console: { log: m => logs.push(m), warn: m => logs.push('WARN ' + m), error: m => logs.push('ERR ' + m) },
    SpreadsheetApp: { getActive: () => ss, flush: () => {}, getUi: () => ({}) },
    PropertiesService: { getScriptProperties: () => ({ getProperty: k => (k in props ? props[k] : null) }) },
    Session: { getScriptTimeZone: () => (props.__scriptTz === undefined ? 'Asia/Colombo' : props.__scriptTz) },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock: () => {} }) },
    UrlFetchApp: { fetch: (url, opts) => db.handle(url, opts) },
    ScriptApp: {},
    Utilities: {
      getUuid: () => crypto.randomUUID(),
      sleep: () => {},
      formatDate: (d, tz, pattern) => {
        if (typeof tz !== 'string' || !tz) throw new Error('Invalid argument: timeZone. Should be of type: String');
        if (pattern === 'yyyy') return String(d.getUTCFullYear());
        assert.strictEqual(pattern, 'yyyy-MM-dd'); return fmt(d, tz);
      },
      computeDigest: (_alg, s) => Array.from(crypto.createHash('sha256').update(s, 'utf8').digest()).map(b => (b > 127 ? b - 256 : b)),
      DigestAlgorithm: { SHA_256: 'SHA_256' },
      Charset: { UTF_8: 'UTF_8' },
    },
    Date, Math, JSON, Number, String, Object, Array, Error, isFinite, isNaN, parseInt, parseFloat, RegExp,
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../apps_script/sync_to_supabase.gs'), 'utf8'), ctx);
  ctx.__logs = logs;
  return ctx;
}

const HEADER = ['Date', 'Location', 'Category', 'Bill No.', 'IMEI', 'Brand/Item', 'Model', 'Capacity', 'Colour', 'Sales Rep', 'Revenue', 'Cost', 'Profit'];
const d = (y, m, dd) => new Date(Date.UTC(y, m - 1, dd, 0, 0, 0) - 330 * 60000); // midnight Asia/Colombo
function sampleRows() {
  return [
    [d(2026, 7, 1), 'ISEVEN', 'ACCESSORIES', 1604, '', 'GREEN 4 IN 1', '', '', '', 'AADHIL', 10000, 7500, 2500],
    [d(2026, 7, 1), 'ISEVEN', 'ACCESSORIES', 1604, '', 'BACKCASE', '', '', '', 'AADHIL', 3000, 2000, 1000],
    [d(2026, 7, 1), 'ISEVEN', 'ACCESSORIES', 1607, '', 'TEMPERED', '', '', '', 'AADHIL', 1000, 150, 850],
    [d(2026, 7, 1), 'ISEVEN', 'ACCESSORIES', 1607, '', 'BACKCASE', '', '', '', 'AADHIL', 1000, 500, 500],
    [d(2026, 7, 1), 'ISEVEN', 'ACCESSORIES', 1608, '', 'ASPOR * A315 10000mah 22.5W POWER BANK', '', '', '', 'AADHIL', 7000, 6500, 500],
    [d(2026, 7, 1), 'iseven ', 'Phones', '1609', 356789012345678, 'APPLE', 'IPHONE 15', '128GB', 'BLACK', 'ABDULLAH', '2,45,000', '2,30,000', 15000],
    ['01/07/2026', 'ISEVEN', 'ACCESSORIES', 1601, '', 'GREEN STEVE', '', '', '', 'ABDULLAH', 'Rs. 5,000', 3000, 2000],
    [d(2026, 7, 1), 'ISEVEN', 'ACCESSORIES', 1603, '', 'CABLE 2M - APPLE', '', '', '', 'ABDULLAH', 10000, 6500, 3500],
  ];
}

let passed = 0;
function test(name, fn) { fn(); passed++; console.log('ok -', name); }

// ---------- tests ----------
test('pure cleaners', () => {
  const ctx = makeContext(new FakeSpreadsheet(), new FakeSupabase(), {});
  assert.strictEqual(ctx.toNumber_('2,45,000'), 245000);
  assert.strictEqual(ctx.toNumber_('Rs. 5,000.50'), 5000.5);
  assert.strictEqual(ctx.toNumber_('(1,000)'), -1000);
  assert.strictEqual(ctx.toNumber_(''), null);
  assert.throws(() => ctx.toNumber_('abc'));
  assert.strictEqual(ctx.toIsoDate_('01/07/2026', 'Asia/Colombo', 'DMY'), '2026-07-01');
  assert.strictEqual(ctx.toIsoDate_('07/01/2026', 'Asia/Colombo', 'MDY'), '2026-07-01');
  assert.strictEqual(ctx.toIsoDate_('2026-07-01', 'Asia/Colombo', 'DMY'), '2026-07-01');
  assert.strictEqual(ctx.toIsoDate_(d(2026, 7, 1), 'Asia/Colombo', 'DMY'), '2026-07-01');
  assert.strictEqual(ctx.toIsoDate_(46204, 'Asia/Colombo', 'DMY'), '2026-07-01'); // sheet serial
  assert.throws(() => ctx.toIsoDate_('31/02/2026', 'Asia/Colombo', 'DMY'), /impossible/);
  assert.throws(() => ctx.toIsoDate_('1999-01-01', 'Asia/Colombo', 'DMY'), /outside/);
  assert.strictEqual(ctx.toText_(356789012345678), '356789012345678');
  assert.strictEqual(ctx.toText_('  iseven  ', true), 'ISEVEN');
  assert.strictEqual(ctx.normHeader_('Bill No.'), 'bill no');
});

test('first sync inserts all rows, assigns ids, cleans values', () => {
  const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
  ss.sheets.Sales = new FakeSheet('Sales', [HEADER.slice(), ...sampleRows()]);
  const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co/', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_abc' });
  const s = ctx.syncSales();
  assert.strictEqual(s.status, 'ok');
  assert.strictEqual(db.tx.size, 8);
  const phone = [...db.tx.values()].find(r => r.bill_no === '1609');
  assert.strictEqual(phone.location, 'ISEVEN');
  assert.strictEqual(phone.category, 'PHONES');
  assert.strictEqual(phone.revenue, 245000);
  assert.strictEqual(phone.imei, '356789012345678');
  assert.strictEqual(phone.sale_date, '2026-07-01');
  assert.ok(!('profit' in phone), 'profit is generated in DB, never sent');
  const call = db.calls.find(c => c.method === 'post');
  assert.strictEqual(call.headers.apikey, 'sb_secret_abc');
  assert.ok(!call.headers.Authorization, 'sb_secret keys must not be sent as Bearer');
  const sheet = ss.sheets.Sales;
  assert.strictEqual(sheet.data[0][13], '_row_id'); assert.strictEqual(sheet.data[0][14], '_sync_hash');
  assert.ok(sheet.data.slice(1).every(r => /^[0-9a-f-]{36}$/.test(r[13]) && /^[0-9a-f]{64}$/.test(r[14])));
  assert.deepStrictEqual(sheet.hidden.sort(), [14, 15]);
});

test('re-run is idempotent and sends nothing; edits send only the changed row', () => {
  const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
  ss.sheets.Sales = new FakeSheet('Sales', [HEADER.slice(), ...sampleRows()]);
  const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'eyJlegacy' });
  ctx.syncSales();
  const ids1 = ss.sheets.Sales.data.slice(1).map(r => r[13]);
  const posts = () => db.calls.filter(c => c.method === 'post' && c.path.endsWith('sales_transactions')).length;
  const before = posts();
  const s2 = ctx.syncSales();
  assert.strictEqual(posts(), before, 'no upsert when nothing changed');
  assert.strictEqual(s2.sheets[0].unchanged, 8);
  assert.deepStrictEqual(ss.sheets.Sales.data.slice(1).map(r => r[13]), ids1, 'ids are stable');
  assert.strictEqual(db.calls[0].headers.Authorization, 'Bearer eyJlegacy', 'legacy JWT also goes in Authorization');
  ss.sheets.Sales.data[3][10] = 1200; // edit revenue of row 3
  const s3 = ctx.syncSales();
  assert.strictEqual(s3.sheets[0].upserted, 1);
  assert.strictEqual(db.tx.size, 8);
  assert.strictEqual(db.tx.get(ids1[2]).revenue, 1200);
});

test('sorting the sheet keeps ids attached to their rows', () => {
  const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
  ss.sheets.Sales = new FakeSheet('Sales', [HEADER.slice(), ...sampleRows()]);
  const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_x' });
  ctx.syncSales();
  const body = ss.sheets.Sales.data.slice(1).reverse();
  ss.sheets.Sales.data = [ss.sheets.Sales.data[0], ...body];
  const s = ctx.syncSales();
  assert.strictEqual(s.sheets[0].upserted, 0);
  assert.strictEqual(db.tx.size, 8);
});

test('copy-pasted row gets a fresh id instead of overwriting the original', () => {
  const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
  ss.sheets.Sales = new FakeSheet('Sales', [HEADER.slice(), ...sampleRows()]);
  const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_x' });
  ctx.syncSales();
  ss.sheets.Sales.data.push(ss.sheets.Sales.data[1].slice()); // duplicate incl. hidden id + hash
  const s = ctx.syncSales();
  assert.strictEqual(s.sheets[0].idsAssigned, 1);
  assert.strictEqual(db.tx.size, 9);
});

test('invalid rows are reported, not sent; header order does not matter', () => {
  const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
  const order = [10, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12];
  const rows = sampleRows();
  rows[1][10] = 'abc';       // bad revenue
  rows[2][3] = '';           // missing bill no -> still VALID (optional)
  rows[3][2] = '';           // missing category
  rows.push(['', '', '', '', '', '', '', '', '', '', '', '', '']); // blank row ignored
  ss.sheets.Sales = new FakeSheet('Sales', [order.map(i => HEADER[i]), ...rows.map(r => order.map(i => r[i]))]);
  const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_x' });
  const s = ctx.syncSales();
  assert.strictEqual(s.sheets[0].invalid, 2);
  assert.strictEqual(db.tx.size, 6);
  const errs = ss.sheets._sync_errors.data.slice(1).map(r => r[2]).sort();
  assert.deepStrictEqual(errs, ['category', 'revenue']);
  assert.ok([...db.tx.values()].some(r => r.bill_no === null), 'row without bill no is synced');
});

test('missing required header fails loudly', () => {
  const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
  ss.sheets.Sales = new FakeSheet('Sales', [HEADER.map(h => (h === 'Revenue' ? 'Rev' : h)), ...sampleRows()]);
  const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_x' });
  assert.throws(() => ctx.syncSales(), /missing required header\(s\): Revenue/);
  assert.strictEqual(ss.sheets._sync_log.data[1][3], 'error');
});

test('retries 503/429 then succeeds; 400 fails fast', () => {
  const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
  ss.sheets.Sales = new FakeSheet('Sales', [HEADER.slice(), ...sampleRows()]);
  const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_x' });
  db.failNext = [503, 429];
  assert.strictEqual(ctx.syncSales().status, 'ok');
  assert.strictEqual(db.tx.size, 8);
  ss.sheets.Sales.data[1][10] = 1;
  db.failNext = [400];
  const n = db.calls.length;
  assert.throws(() => ctx.syncSales(), /HTTP 400/);
  assert.strictEqual(db.calls.length - n, 1, 'no retry on 400');
});

test('reconcile soft-deletes rows removed from the sheet', () => {
  const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
  ss.sheets.Sales = new FakeSheet('Sales', [HEADER.slice(), ...sampleRows()]);
  const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_x' });
  ctx.syncSales();
  const removedId = ss.sheets.Sales.data[2][13];
  ss.sheets.Sales.data.splice(2, 1);
  const s = ctx.reconcileSales();
  assert.match(s.message, /"soft_deleted":1/);
  assert.strictEqual(db.tx.get(removedId).deleted_at, 'now');
  assert.strictEqual([...db.tx.values()].filter(r => !r.deleted_at).length, 7);
});

test('multiple tabs via SHEET_NAMES', () => {
  const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
  ss.sheets.Jul = new FakeSheet('Jul', [HEADER.slice(), ...sampleRows().slice(0, 4)]);
  ss.sheets.Aug = new FakeSheet('Aug', [HEADER.slice(), ...sampleRows().slice(4)]);
  const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_x', SHEET_NAMES: 'Jul, Aug' });
  ctx.syncSales();
  assert.strictEqual(db.tx.size, 8);
  assert.deepStrictEqual([...new Set([...db.tx.values()].map(r => r.source_sheet))].sort(), ['Aug', 'Jul']);
});

test('real date cells work when the spreadsheet time zone is missing', () => {
  for (const sheetTz of [undefined, '', null]) {
    const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
    ss.getSpreadsheetTimeZone = () => sheetTz;
    ss.sheets.Sales = new FakeSheet('Sales', [HEADER.slice(), ...sampleRows()]);
    const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_x' });
    const s = ctx.syncSales();
    assert.strictEqual(s.sheets[0].invalid, 0, 'no date errors');
    assert.ok([...db.tx.values()].every(r => r.sale_date === '2026-07-01'));
  }
  // both sources broken -> Asia/Colombo
  const ss = new FakeSpreadsheet(); const db = new FakeSupabase();
  ss.getSpreadsheetTimeZone = () => undefined;
  ss.sheets.Sales = new FakeSheet('Sales', [HEADER.slice(), ...sampleRows()]);
  const ctx = makeContext(ss, db, { SUPABASE_URL: 'https://x.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_x', __scriptTz: '' });
  assert.strictEqual(ctx.sheetTimeZone_(), 'Asia/Colombo');
  assert.strictEqual(ctx.syncSales().sheets[0].invalid, 0);
});

console.log(`\n${passed} Apps Script tests passed`);
