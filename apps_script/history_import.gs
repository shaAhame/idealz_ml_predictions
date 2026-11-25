/**
 * history_import.gs — one-time import of the old Excel sales history
 * =============================================================================
 * Add this as a SECOND script file next to sync_to_supabase.gs (same project):
 *   Apps Script editor > Files "+" > Script > name it "history_import" > paste.
 *
 * Steps
 *   1. Import the Excel file as-is into this spreadsheet as a tab named
 *      "excel_raw" (File > Import > Upload > Insert new sheet(s), then rename).
 *   2. Run buildHistory. It creates:
 *        history        clean rows in the live sheet's format, only for dates
 *                       HISTORY.START .. day before HISTORY.END_EXCLUSIVE
 *        history_check  summary numbers + problem rows (no customer details)
 *   3. Review history_check. Then add "history" to the SHEET_NAMES script
 *      property (e.g. "profit, history") and run syncSales.
 *
 * Nothing here talks to Supabase; it only reads excel_raw and writes two tabs.
 * Reuses helpers from sync_to_supabase.gs (toIsoDate_, toNumber_, toText_,
 * normHeader_, resolveColumns_, cleanRow_, sheetTimeZone_, getConfig_).
 * =============================================================================
 */

var HISTORY = {
  RAW_SHEET: 'excel_raw',
  OUT_SHEET: 'history',
  CHECK_SHEET: 'history_check',
  START: '2025-08-19',          // first day to import (inclusive)
  END_EXCLUSIVE: '2026-07-01',  // rows on/after this date are already in the live tab
  RAW_DATE_ORDER: 'MDY',        // Excel shows 8-19-2025 = month-day-year
  MAX_PROBLEM_ROWS: 200
};

// Output header (must match COLUMN_MAP in sync_to_supabase.gs) -> candidate Excel
// headers, normalised (lower case, single spaces, no trailing "."). First match wins.
var HISTORY_COLUMNS = [
  ['Date',       ['date']],
  ['Location',   ['location']],
  ['Category',   ['category']],
  ['Bill No.',   ['bill no', 'bill number', 'bill']],
  ['IMEI',       ['imei']],
  ['Brand/Item', ['clean brand', 'brand', 'brand/item', 'item']],
  ['Model',      ['clean model', 'model']],
  ['Capacity',   ['clean capacity', 'capacity']],
  ['Colour',     ['clean color', 'clean colour', 'colour', 'color']],
  ['Sales Rep',  ['clean sales rep', 'sales rep']],
  ['Revenue',    ['total', 'revenue']],
  ['Cost',       ['cost']]
];
var HISTORY_REQUIRED = ['Date', 'Location', 'Category', 'Revenue'];
var PAYMENT_HEADERS = ['cash', 'sampath', 'amana', 'bank', 'seylan', 'commercial', 'commercial bank', 'amex', 'web'];

function buildHistory() {
  var ss = SpreadsheetApp.getActive();
  var raw = ss.getSheetByName(HISTORY.RAW_SHEET);
  if (!raw) throw new Error('Tab "' + HISTORY.RAW_SHEET + '" not found. Import the Excel file and rename its tab to ' + HISTORY.RAW_SHEET + '.');

  var existing = ss.getSheetByName(HISTORY.OUT_SHEET);
  if (existing && existing.getLastRow() > 1) {
    throw new Error('Tab "' + HISTORY.OUT_SHEET + '" already has data. Rebuilding would give every row a new id and ' +
      'double-count sales in Supabase. If you really need to rebuild: delete the "' + HISTORY.OUT_SHEET +
      '" tab, run buildHistory, then run syncSales AND reconcileSales once.');
  }

  var tz = sheetTimeZone_();
  var lastRow = raw.getLastRow(), lastCol = raw.getLastColumn();
  if (lastRow < 2) throw new Error('Tab "' + HISTORY.RAW_SHEET + '" is empty.');
  var headers = raw.getRange(1, 1, 1, lastCol).getValues()[0];
  var values = raw.getRange(2, 1, lastRow - 1, lastCol).getValues();

  // --- map headers -------------------------------------------------------------
  var lookup = {};
  headers.forEach(function (h, i) { var k = normHeader_(h); if (k && lookup[k] === undefined) lookup[k] = i; });
  var idx = {};
  var missing = [];
  HISTORY_COLUMNS.forEach(function (pair) {
    for (var j = 0; j < pair[1].length; j++) {
      if (lookup[pair[1][j]] !== undefined) { idx[pair[0]] = lookup[pair[1][j]]; break; }
    }
    if (idx[pair[0]] === undefined && HISTORY_REQUIRED.indexOf(pair[0]) >= 0) missing.push(pair[0]);
  });
  if (missing.length) {
    throw new Error('Could not find column(s) for: ' + missing.join(', ') + '. Headers found in ' + HISTORY.RAW_SHEET +
      ': ' + headers.map(String).filter(String).join(' | '));
  }
  var payIdx = PAYMENT_HEADERS.map(function (h) { return lookup[h]; }).filter(function (i) { return i !== undefined; });

  // --- convert -----------------------------------------------------------------
  var out = [];
  var s = { raw: 0, blank: 0, kept: 0, overlap: 0, beforeStart: 0, invalid: 0, zero: 0, negative: 0, payMismatch: 0,
            minDate: null, maxDate: null };
  var problems = [];
  var byMonth = {}, byGroup = {}, overlapMonth = {};
  function problem(row, reason, value) {
    if (problems.length < HISTORY.MAX_PROBLEM_ROWS) problems.push([row, reason, value === undefined ? '' : String(value)]);
  }

  for (var r = 0; r < values.length; r++) {
    var row = values[r];
    var sheetRow = r + 2;
    var blank = HISTORY_REQUIRED.every(function (k) { var v = row[idx[k]]; return v === '' || v === null; });
    if (blank) { s.blank++; continue; }
    s.raw++;

    var date, total;
    try { date = toIsoDate_(row[idx['Date']], tz, HISTORY.RAW_DATE_ORDER); }
    catch (e) { s.invalid++; problem(sheetRow, 'bad date: ' + e.message, row[idx['Date']]); continue; }
    try { total = toNumber_(row[idx['Revenue']]); }
    catch (e2) { s.invalid++; problem(sheetRow, 'bad TOTAL: ' + e2.message, row[idx['Revenue']]); continue; }
    var loc = toText_(row[idx['Location']], true), cat = toText_(row[idx['Category']], true);
    if (!date || total === null || !loc || !cat) {
      s.invalid++;
      problem(sheetRow, 'missing ' + [!date && 'date', total === null && 'TOTAL', !loc && 'location', !cat && 'category']
        .filter(String).join(', '));
      continue;
    }

    var month = date.slice(0, 7);
    if (date >= HISTORY.END_EXCLUSIVE) {   // already in the live tab: compare, don't import
      s.overlap++;
      overlapMonth[month] = (overlapMonth[month] || 0) + total;
      continue;
    }
    if (date < HISTORY.START) { s.beforeStart++; problem(sheetRow, 'date before ' + HISTORY.START, date); continue; }

    if (payIdx.length) {
      var paid = 0;
      payIdx.forEach(function (i) { try { paid += toNumber_(row[i]) || 0; } catch (e3) { /* ignore */ } });
      if (Math.abs(paid - total) > 1) { s.payMismatch++; problem(sheetRow, 'TOTAL differs from sum of payment columns', total + ' vs ' + paid); }
    }
    if (total === 0) s.zero++;
    if (total < 0) s.negative++;

    s.kept++;
    if (!s.minDate || date < s.minDate) s.minDate = date;
    if (!s.maxDate || date > s.maxDate) s.maxDate = date;
    byMonth[month] = byMonth[month] || [0, 0]; byMonth[month][0]++; byMonth[month][1] += total;
    var g = loc + ' | ' + cat;
    byGroup[g] = byGroup[g] || [0, 0]; byGroup[g][0]++; byGroup[g][1] += total;

    var cost = null;
    if (idx['Cost'] !== undefined) { try { cost = toNumber_(row[idx['Cost']]); } catch (e4) { cost = null; } }
    out.push([
      date, loc, cat,
      idx['Bill No.'] === undefined ? '' : (toText_(row[idx['Bill No.']], false) || ''),
      idx['IMEI'] === undefined ? '' : (toText_(row[idx['IMEI']], false) || ''),
      idx['Brand/Item'] === undefined ? '' : (toText_(row[idx['Brand/Item']], false) || ''),
      idx['Model'] === undefined ? '' : (toText_(row[idx['Model']], false) || ''),
      idx['Capacity'] === undefined ? '' : (toText_(row[idx['Capacity']], false) || ''),
      idx['Colour'] === undefined ? '' : (toText_(row[idx['Colour']], false) || ''),
      idx['Sales Rep'] === undefined ? '' : (toText_(row[idx['Sales Rep']], false) || ''),
      total,
      cost === null ? '' : cost
    ]);
  }

  // --- write history -------------------------------------------------------------
  var outSheet = existing || ss.insertSheet(HISTORY.OUT_SHEET);
  outSheet.clear();
  var outHeaders = HISTORY_COLUMNS.map(function (p) { return p[0]; });
  outSheet.getRange(1, 1, 1, outHeaders.length).setValues([outHeaders]).setFontWeight('bold');
  if (out.length) {
    // Dates and ids as plain text so Sheets never re-interprets them (2025-08-05 stays 5 August).
    outSheet.getRange(2, 1, out.length, 1).setNumberFormat('@');
    outSheet.getRange(2, 4, out.length, 2).setNumberFormat('@');
    outSheet.getRange(2, 1, out.length, outHeaders.length).setValues(out);
  }
  outSheet.setFrozenRows(1);

  // --- write history_check ---------------------------------------------------------
  var live = liveMonthlyRevenue_(tz);
  writeHistoryCheck_(s, byMonth, byGroup, overlapMonth, live, problems, payIdx.length > 0);
  SpreadsheetApp.flush();
  var summary = 'history: ' + s.kept + ' rows (' + s.minDate + ' .. ' + s.maxDate + '), skipped ' + s.overlap +
    ' rows already in the live tab, ' + s.invalid + ' invalid, ' + s.beforeStart + ' before ' + HISTORY.START +
    '. See the ' + HISTORY.CHECK_SHEET + ' tab.';
  console.log(summary);
  return s;
}

/** Revenue per month in the live tab(s) from SHEET_NAMES (excluding history), for the overlap check. */
function liveMonthlyRevenue_(tz) {
  var res = {};
  var names;
  try { names = getConfig_().sheetNames; } catch (e) { names = ['profit']; }
  names.filter(function (n) { return n !== HISTORY.OUT_SHEET; }).forEach(function (name) {
    var sh = SpreadsheetApp.getActive().getSheetByName(name);
    if (!sh || sh.getLastRow() < 2) return;
    var headers = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
    var colIndex;
    try { colIndex = resolveColumns_(headers, name); } catch (e) { return; }
    var vals = sh.getRange(2, 1, sh.getLastRow() - 1, sh.getLastColumn()).getValues();
    vals.forEach(function (row) {
      if (isBlankRow_(row, colIndex)) return;
      var c = cleanRow_(row, colIndex, tz, getStringDateOrder_());
      if (c.errors.length) return;
      var m = c.record.sale_date.slice(0, 7);
      res[m] = (res[m] || 0) + c.record.revenue;
    });
  });
  return res;
}

function getStringDateOrder_() {
  try { return getConfig_().dateOrder; } catch (e) { return 'DMY'; }
}

function writeHistoryCheck_(s, byMonth, byGroup, overlapMonth, live, problems, havePayments) {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(HISTORY.CHECK_SHEET) || ss.insertSheet(HISTORY.CHECK_SHEET);
  sh.clear();
  var rows = [];
  var add = function (a) { rows.push(a.concat(['', '', '', '']).slice(0, 4)); };
  add(['HISTORY IMPORT CHECK', 'built ' + new Date().toISOString()]);
  add(['']);
  add(['Summary', 'Value']);
  add(['Rows imported into "history"', s.kept]);
  add(['Date range imported', (s.minDate || '-') + ' to ' + (s.maxDate || '-')]);
  add(['Skipped: on/after ' + HISTORY.END_EXCLUSIVE + ' (already in live tab)', s.overlap]);
  add(['Skipped: before ' + HISTORY.START, s.beforeStart]);
  add(['Skipped: invalid (see problem rows)', s.invalid]);
  add(['Rows with TOTAL = 0', s.zero]);
  add(['Rows with negative TOTAL (returns)', s.negative]);
  add(['Rows where TOTAL != sum of payments', havePayments ? s.payMismatch : 'no payment columns found']);
  add(['']);
  add(['Overlap check: Excel vs live sheet', 'Excel revenue', 'Live sheet revenue', 'Difference %']);
  Object.keys(overlapMonth).sort().forEach(function (m) {
    var e = overlapMonth[m], l = live[m] || 0;
    add([m, Math.round(e), Math.round(l), l ? Math.round((e - l) / l * 1000) / 10 : 'n/a']);
  });
  add(['']);
  add(['Imported per month', 'Rows', 'Revenue']);
  Object.keys(byMonth).sort().forEach(function (m) { add([m, byMonth[m][0], Math.round(byMonth[m][1])]); });
  add(['']);
  add(['Imported per location | category', 'Rows', 'Revenue']);
  Object.keys(byGroup).sort().forEach(function (g) { add([g, byGroup[g][0], Math.round(byGroup[g][1])]); });
  add(['']);
  add(['Problem rows (excel_raw row number)', 'Reason', 'Value']);
  if (!problems.length) add(['none']);
  problems.forEach(function (p) { add([p[0], p[1], p[2]]); });
  sh.getRange(1, 1, rows.length, 4).setValues(rows);
  sh.getRange(1, 1).setFontWeight('bold');
  sh.setColumnWidth(1, 360);
}
