/**
 * sync_to_supabase.gs — Google Sheets -> Supabase idempotent sync
 * =============================================================================
 *
 * What it does
 *   1. Reads every configured tab, maps columns BY HEADER NAME (column order does
 *      not matter), validates and cleans each row.
 *   2. Gives every row a permanent UUID in a hidden `_row_id` column. "Bill No."
 *      is not unique (one bill = several line items), so this UUID is the
 *      upsert key. It survives sorting, filtering and edits.
 *   3. Hashes each cleaned row; in incremental mode only rows whose hash changed
 *      since the last successful sync (hidden `_sync_hash` column) are sent.
 *   4. Upserts in batches to POST /rest/v1/sales_transactions?on_conflict=row_id
 *      with `Prefer: resolution=merge-duplicates` -> re-running is idempotent.
 *   5. reconcileSales() (nightly) also soft-deletes DB rows whose sheet row was
 *      deleted, with a safety guard against mass deletion.
 *
 * Setup (once)
 *   Extensions > Apps Script > paste this file (+ appsscript.json).
 *   Project Settings > Script properties:
 *     SUPABASE_URL               https://<project-ref>.supabase.co          (required)
 *     SUPABASE_SERVICE_ROLE_KEY  sb_secret_... (or legacy service_role JWT)  (required)
 *     SHEET_NAMES                Sales            comma-separated tab names  (default "Sales")
 *     HEADER_ROW                 1                                           (default 1)
 *     BATCH_SIZE                 500                                         (default 500)
 *     STRING_DATE_ORDER          DMY | MDY | YMD  only used for TEXT dates   (default DMY)
 *     RECONCILE_MIN_RATIO        0.5                                         (default 0.5)
 *   Then run `testConnection`, then `syncSales`, then `installTriggers`.
 *
 * Secrets are read from PropertiesService only. Never paste keys into this file.
 * The service key bypasses Row Level Security: restrict who can edit this script.
 * =============================================================================
 */

// -----------------------------------------------------------------------------
// CUSTOMIZE: database column -> sheet header. Header matching ignores case,
// extra spaces and a trailing "." ("Bill No." == "bill no").
// -----------------------------------------------------------------------------
var COLUMN_MAP = {
  sale_date: 'Date',
  location:  'Location',
  category:  'Category',
  bill_no:   'Bill No.',
  imei:      'IMEI',
  item:      'Brand/Item',
  model:     'Model',
  capacity:  'Capacity',
  colour:    'Colour',
  sales_rep: 'Sales Rep',
  revenue:   'Revenue',
  cost:      'Cost'
  // "Profit" is intentionally not synced: the DB computes profit = revenue - cost.
};

// Bill No. is optional: some sales lines have no bill number, and their revenue still counts.
var REQUIRED_FIELDS = ['sale_date', 'location', 'category', 'revenue'];
var UPPERCASE_FIELDS = ['location', 'category'];  // normalised so series keys are stable
var NUMERIC_FIELDS = ['revenue', 'cost'];

var TABLE = 'sales_transactions';
var ROW_ID_HEADER = '_row_id';
var HASH_HEADER = '_sync_hash';
var ERRORS_SHEET = '_sync_errors';
var LOG_SHEET = '_sync_log';
var MAX_LOG_ROWS = 2000;
var MAX_RUNTIME_MS = 5 * 60 * 1000;        // Apps Script hard limit is 6 min
var MAX_ATTEMPTS = 5;
var SEEN_CHUNK = 5000;

// -----------------------------------------------------------------------------
// Public entry points
// -----------------------------------------------------------------------------

/** Incremental sync: sends new/changed rows only. Run every 15 minutes. */
function syncSales() {
  return runSync_({ mode: 'incremental' });
}

/**
 * Reconcile: sends changed rows AND soft-deletes DB rows whose sheet row was deleted. Run nightly.
 * (To force every row to be re-sent, run resetSyncHashes first.)
 */
function reconcileSales() {
  return runSync_({ mode: 'reconcile' });
}

/** Forces every row to be re-sent on the next sync (clears _sync_hash). */
function resetSyncHashes() {
  var cfg = getConfig_();
  cfg.sheetNames.forEach(function (name) {
    var sheet = SpreadsheetApp.getActive().getSheetByName(name);
    if (!sheet) return;
    var meta = ensureMetaColumns_(sheet, cfg.headerRow);
    var n = sheet.getLastRow() - cfg.headerRow;
    if (n > 0) sheet.getRange(cfg.headerRow + 1, meta.hashCol, n, 1).clearContent();
  });
  console.log('Sync hashes cleared; next sync re-sends all rows.');
}

/** Verifies URL + key + table access without writing anything. */
function testConnection() {
  var cfg = getConfig_();
  var res = request_(cfg, 'get', '/rest/v1/' + TABLE + '?select=row_id&limit=1', null, {});
  console.log('Connection OK (HTTP ' + res.getResponseCode() + ').');
  return true;
}

/** Installs the 15-minute incremental trigger and the nightly reconcile trigger. */
function installTriggers() {
  uninstallTriggers();
  ScriptApp.newTrigger('syncSales').timeBased().everyMinutes(15).create();
  ScriptApp.newTrigger('reconcileSales').timeBased().everyDays(1).atHour(2).create();
  console.log('Triggers installed: syncSales every 15 min, reconcileSales daily ~02:00.');
}

function uninstallTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    var fn = t.getHandlerFunction();
    if (fn === 'syncSales' || fn === 'reconcileSales') ScriptApp.deleteTrigger(t);
  });
}

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Supabase Sync')
    .addItem('Sync now', 'syncSales')
    .addItem('Full reconcile (incl. deletes)', 'reconcileSales')
    .addItem('Test connection', 'testConnection')
    .addSeparator()
    .addItem('Install triggers', 'installTriggers')
    .addItem('Force full re-send on next sync', 'resetSyncHashes')
    .addToUi();
}

// -----------------------------------------------------------------------------
// Orchestration
// -----------------------------------------------------------------------------

function runSync_(opts) {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30 * 1000)) {
    console.warn('Another sync is running; skipping this invocation.');
    return null;
  }
  var started = Date.now();
  var runId = Utilities.getUuid();
  var cfg = getConfig_();
  var summary = { runId: runId, mode: opts.mode, sheets: [], status: 'ok', message: '' };
  var allErrors = [];
  var seenIds = [];
  var seenSet = {};
  var complete = true;

  try {
    for (var i = 0; i < cfg.sheetNames.length; i++) {
      var name = cfg.sheetNames[i];
      var sheet = SpreadsheetApp.getActive().getSheetByName(name);
      if (!sheet) throw new Error('Tab "' + name + '" not found (check SHEET_NAMES).');
      var res = processSheet_(sheet, cfg, opts.mode, started, seenSet);
      summary.sheets.push(res.stats);
      allErrors = allErrors.concat(res.errors);
      seenIds = seenIds.concat(res.seenIds);
      if (!res.complete) complete = false;
    }

    if (opts.mode === 'reconcile') {
      if (!complete) {
        summary.message = 'Reconcile skipped: not all rows were upserted in this run (time limit). ';
      } else {
        var rec = reconcileDeletes_(cfg, runId, seenIds);
        summary.message = 'Reconcile: ' + JSON.stringify(rec) + ' ';
      }
    }
    if (!complete) summary.status = 'partial';
  } catch (e) {
    summary.status = 'error';
    summary.message += String(e && e.stack || e);
    console.error(summary.message);
    throw e;
  } finally {
    summary.durationMs = Date.now() - started;
    writeErrorsSheet_(allErrors);
    appendLog_(summary);
    lock.releaseLock();
    console.log(JSON.stringify(summary));
  }
  return summary;
}

/**
 * Reads, validates, assigns ids, upserts one tab.
 * Returns { stats, errors, seenIds, complete }.
 */
function processSheet_(sheet, cfg, mode, started, seenSet) {
  var name = sheet.getName();
  var stats = { sheet: name, scanned: 0, valid: 0, invalid: 0, unchanged: 0, upserted: 0, idsAssigned: 0 };
  var errors = [];
  var seenIds = [];

  var meta = ensureMetaColumns_(sheet, cfg.headerRow);
  var lastRow = sheet.getLastRow();
  var n = lastRow - cfg.headerRow;
  if (n <= 0) return { stats: stats, errors: errors, seenIds: seenIds, complete: true };

  var lastCol = sheet.getLastColumn();
  var headers = sheet.getRange(cfg.headerRow, 1, 1, lastCol).getValues()[0];
  var colIndex = resolveColumns_(headers, name);
  var values = sheet.getRange(cfg.headerRow + 1, 1, n, lastCol).getValues();
  var fingerprint = fingerprint_(values, colIndex);
  var tz = sheetTimeZone_();

  var ids = new Array(n);
  var hashes = new Array(n);
  var idsChanged = false;
  var pending = [];

  for (var r = 0; r < n; r++) {
    var row = values[r];
    var sheetRow = cfg.headerRow + 1 + r;
    var currentId = String(row[meta.rowIdCol - 1] || '').trim();
    hashes[r] = String(row[meta.hashCol - 1] || '');

    if (isBlankRow_(row, colIndex)) { ids[r] = currentId; continue; }
    stats.scanned++;

    // Assign a permanent id; regenerate on duplicates (copy-pasted rows).
    if (!currentId || seenSet[currentId]) {
      currentId = Utilities.getUuid();
      idsChanged = true;
      stats.idsAssigned++;
      hashes[r] = '';
    }
    ids[r] = currentId;
    seenSet[currentId] = true;
    seenIds.push(currentId);

    var result = cleanRow_(row, colIndex, tz, cfg.dateOrder);
    if (result.errors.length) {
      stats.invalid++;
      result.errors.forEach(function (err) {
        errors.push([name, sheetRow, err.field, err.message, String(err.value)]);
      });
      continue;
    }
    stats.valid++;

    var record = result.record;
    var hash = sha256Hex_(canonicalJson_(record));
    if (hash === hashes[r]) { stats.unchanged++; continue; }  // both modes: only changed rows are sent

    record.row_id = currentId;
    record.row_hash = hash;
    record.source_sheet = name;
    record.deleted_at = null;  // un-delete if the row re-appears
    pending.push({ index: r, record: record, hash: hash });
  }

  // Persist new ids BEFORE upserting, so a crash can never orphan DB rows.
  if (idsChanged) {
    assertSheetUnchanged_(sheet, cfg, lastRow, lastCol, colIndex, fingerprint);
    sheet.getRange(cfg.headerRow + 1, meta.rowIdCol, n, 1).setValues(ids.map(function (v) { return [v]; }));
    SpreadsheetApp.flush();
  }

  var complete = true;
  for (var start = 0; start < pending.length; start += cfg.batchSize) {
    if (Date.now() - started > MAX_RUNTIME_MS) {
      complete = false;
      console.warn('Time budget reached in "' + name + '"; remaining rows go in the next run.');
      break;
    }
    var batch = pending.slice(start, start + cfg.batchSize);
    upsertBatch_(cfg, batch.map(function (p) { return p.record; }));
    batch.forEach(function (p) { hashes[p.index] = p.hash; });
    stats.upserted += batch.length;
  }

  // Save hashes of successfully-upserted rows. If the sheet changed meanwhile we
  // skip it: the only cost is re-sending those rows next time (idempotent).
  if (stats.upserted > 0) {
    try {
      assertSheetUnchanged_(sheet, cfg, lastRow, lastCol, colIndex, fingerprint);
      sheet.getRange(cfg.headerRow + 1, meta.hashCol, n, 1).setValues(hashes.map(function (v) { return [v]; }));
    } catch (e) {
      console.warn('Hash write-back skipped for "' + name + '": ' + e.message);
    }
  }
  return { stats: stats, errors: errors, seenIds: seenIds, complete: complete };
}

// -----------------------------------------------------------------------------
// Cleaning & validation (pure functions — unit-tested in tests/apps_script)
// -----------------------------------------------------------------------------

function cleanRow_(row, colIndex, tz, dateOrder) {
  var record = {};
  var errors = [];
  Object.keys(COLUMN_MAP).forEach(function (field) {
    var idx = colIndex[field];
    var raw = idx === undefined ? '' : row[idx];
    var value;
    try {
      if (field === 'sale_date') value = toIsoDate_(raw, tz, dateOrder);
      else if (NUMERIC_FIELDS.indexOf(field) >= 0) value = toNumber_(raw);
      else value = toText_(raw, UPPERCASE_FIELDS.indexOf(field) >= 0);
    } catch (e) {
      errors.push({ field: field, message: e.message, value: raw });
      return;
    }
    if (value === null && REQUIRED_FIELDS.indexOf(field) >= 0) {
      errors.push({ field: field, message: 'required value is empty', value: raw });
      return;
    }
    record[field] = value;
  });
  return { record: record, errors: errors };
}

function toText_(v, upper) {
  if (v === null || v === undefined) return null;
  var s;
  if (typeof v === 'number') {
    s = Number.isInteger(v) ? v.toFixed(0) : String(v);  // avoids "1.604e3" / keeps IMEIs intact
  } else if (Object.prototype.toString.call(v) === '[object Date]') {
    s = v.toISOString();
  } else {
    s = String(v);
  }
  s = s.replace(/\s+/g, ' ').trim();
  if (s === '') return null;
  return upper ? s.toUpperCase() : s;
}

function toNumber_(v) {
  if (v === null || v === undefined || v === '') return null;
  var n;
  if (typeof v === 'number') {
    n = v;
  } else {
    var s = String(v).trim();
    if (s === '' || s === '-') return null;
    var negative = /^\(.*\)$/.test(s);
    s = s.replace(/^\(|\)$/g, '')
         .replace(/(LKR|Rs\.?|රු\.?|USD|\$)/gi, '')
         .replace(/[,\s]/g, '');
    if (!/^-?\d+(\.\d+)?$/.test(s)) throw new Error('not a number');
    n = Number(s);
    if (negative) n = -n;
  }
  if (!isFinite(n)) throw new Error('not a finite number');
  return Math.round(n * 100) / 100;
}

function toIsoDate_(v, tz, order) {
  if (v === null || v === undefined || v === '') return null;
  var y, m, d;
  if (Object.prototype.toString.call(v) === '[object Date]') {
    if (isNaN(v.getTime())) throw new Error('invalid date');
    return checkYear_(Utilities.formatDate(v, tz, 'yyyy-MM-dd'));
  }
  if (typeof v === 'number') {
    // Sheets serial date (days since 1899-12-30), e.g. a date cell formatted as number.
    if (v < 20000 || v > 80000) throw new Error('number is not a plausible date serial');
    var ms = Math.round((v - 25569) * 86400 * 1000);
    return checkYear_(Utilities.formatDate(new Date(ms), 'UTC', 'yyyy-MM-dd'));
  }
  var s = String(v).trim();
  var iso = s.match(/^(\d{4})[-\/.](\d{1,2})[-\/.](\d{1,2})(?:[ T].*)?$/);
  if (iso) {
    y = +iso[1]; m = +iso[2]; d = +iso[3];
  } else {
    var parts = s.match(/^(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{2,4})$/);
    if (!parts) throw new Error('unrecognised date format');
    var a = +parts[1], b = +parts[2], c = +parts[3];
    if (c < 100) c += 2000;
    if (order === 'MDY') { m = a; d = b; y = c; }
    else if (order === 'YMD') { y = a + 2000; m = b; d = c; }
    else { d = a; m = b; y = c; }
  }
  var dt = new Date(Date.UTC(y, m - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) {
    throw new Error('impossible calendar date');
  }
  return checkYear_(y + '-' + pad2_(m) + '-' + pad2_(d));
}

/**
 * Time zone used to turn date cells into calendar dates. getSpreadsheetTimeZone()
 * can return an empty/non-string value for some files (e.g. converted from Excel),
 * so validate it and fall back to the script's time zone, then Asia/Colombo.
 */
function sheetTimeZone_() {
  var candidates = [];
  try { candidates.push(SpreadsheetApp.getActive().getSpreadsheetTimeZone()); } catch (e) {}
  try { candidates.push(Session.getScriptTimeZone()); } catch (e) {}
  candidates.push('Asia/Colombo');
  for (var i = 0; i < candidates.length; i++) {
    var tz = candidates[i] ? String(candidates[i]) : '';
    if (!tz) continue;
    try { Utilities.formatDate(new Date(), tz, 'yyyy'); return tz; } catch (e) {}
  }
  return 'UTC';
}

function checkYear_(iso) {
  var y = +iso.slice(0, 4);
  var maxY = new Date().getFullYear() + 1;
  if (y < 2000 || y > maxY) throw new Error('date year ' + y + ' outside 2000..' + maxY);
  return iso;
}

function pad2_(x) { return (x < 10 ? '0' : '') + x; }

function isBlankRow_(row, colIndex) {
  return Object.keys(colIndex).every(function (f) {
    var v = row[colIndex[f]];
    return v === '' || v === null || v === undefined;
  });
}

function canonicalJson_(record) {
  var keys = Object.keys(record).sort();
  return JSON.stringify(keys.map(function (k) { return [k, record[k]]; }));
}

function sha256Hex_(s) {
  var bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, s, Utilities.Charset.UTF_8);
  return bytes.map(function (b) { var h = (b & 0xff).toString(16); return h.length === 1 ? '0' + h : h; }).join('');
}

function normHeader_(h) {
  return String(h || '').toLowerCase().replace(/\s+/g, ' ').trim().replace(/\.$/, '');
}

function resolveColumns_(headers, sheetName) {
  var lookup = {};
  headers.forEach(function (h, i) { var k = normHeader_(h); if (k && lookup[k] === undefined) lookup[k] = i; });
  var colIndex = {};
  var missing = [];
  Object.keys(COLUMN_MAP).forEach(function (field) {
    var idx = lookup[normHeader_(COLUMN_MAP[field])];
    if (idx === undefined) {
      if (REQUIRED_FIELDS.indexOf(field) >= 0) missing.push(COLUMN_MAP[field]);
    } else {
      colIndex[field] = idx;
    }
  });
  if (missing.length) {
    throw new Error('Tab "' + sheetName + '" is missing required header(s): ' + missing.join(', ') +
                    '. Fix the sheet or COLUMN_MAP.');
  }
  return colIndex;
}

// -----------------------------------------------------------------------------
// Sheet helpers
// -----------------------------------------------------------------------------

function ensureMetaColumns_(sheet, headerRow) {
  var lastCol = Math.max(sheet.getLastColumn(), 1);
  var headers = sheet.getRange(headerRow, 1, 1, lastCol).getValues()[0].map(normHeader_);
  var rowIdCol = headers.indexOf(ROW_ID_HEADER) + 1;
  var hashCol = headers.indexOf(HASH_HEADER) + 1;
  var needed = (rowIdCol ? 0 : 1) + (hashCol ? 0 : 1);
  if (needed) {
    if (sheet.getMaxColumns() < lastCol + needed) {
      sheet.insertColumnsAfter(sheet.getMaxColumns(), lastCol + needed - sheet.getMaxColumns());
    }
    var next = lastCol + 1;
    if (!rowIdCol) { rowIdCol = next++; sheet.getRange(headerRow, rowIdCol).setValue(ROW_ID_HEADER); sheet.hideColumns(rowIdCol); }
    if (!hashCol) { hashCol = next++; sheet.getRange(headerRow, hashCol).setValue(HASH_HEADER); sheet.hideColumns(hashCol); }
    SpreadsheetApp.flush();
  }
  return { rowIdCol: rowIdCol, hashCol: hashCol };
}

/** Cheap fingerprint of the key columns: detects inserted/deleted/re-sorted rows. */
function fingerprint_(values, colIndex) {
  var keyCols = ['sale_date', 'bill_no', 'revenue'].map(function (f) { return colIndex[f]; });
  var s = values.length + '|' + values.map(function (row) {
    return keyCols.map(function (c) { return String(row[c]); }).join('\u0001');
  }).join('\u0002');
  return sha256Hex_(s);
}

function assertSheetUnchanged_(sheet, cfg, lastRow, lastCol, colIndex, fingerprint) {
  if (sheet.getLastRow() !== lastRow) {
    throw new Error('Sheet "' + sheet.getName() + '" changed during sync (row count); aborting, will retry next run.');
  }
  var now = sheet.getRange(cfg.headerRow + 1, 1, lastRow - cfg.headerRow, lastCol).getValues();
  if (fingerprint_(now, colIndex) !== fingerprint) {
    throw new Error('Sheet "' + sheet.getName() + '" changed during sync (rows moved); aborting, will retry next run.');
  }
}

function writeErrorsSheet_(errors) {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(ERRORS_SHEET) || ss.insertSheet(ERRORS_SHEET);
  sh.clearContents();
  var header = [['Tab', 'Row', 'Field', 'Problem', 'Value', 'Checked at']];
  sh.getRange(1, 1, 1, 6).setValues(header).setFontWeight('bold');
  if (!errors.length) return;
  var ts = new Date();
  var rows = errors.slice(0, 5000).map(function (e) { return e.concat([ts]); });
  sh.getRange(2, 1, rows.length, 6).setValues(rows);
}

function appendLog_(summary) {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(LOG_SHEET);
  if (!sh) {
    sh = ss.insertSheet(LOG_SHEET);
    sh.appendRow(['Time', 'Run id', 'Mode', 'Status', 'Scanned', 'Valid', 'Invalid', 'Unchanged',
                  'Upserted', 'New ids', 'Duration (s)', 'Message']);
  }
  var t = { scanned: 0, valid: 0, invalid: 0, unchanged: 0, upserted: 0, idsAssigned: 0 };
  summary.sheets.forEach(function (s) { Object.keys(t).forEach(function (k) { t[k] += s[k]; }); });
  sh.appendRow([new Date(), summary.runId, summary.mode, summary.status, t.scanned, t.valid,
                t.invalid, t.unchanged, t.upserted, t.idsAssigned,
                Math.round(summary.durationMs / 100) / 10, summary.message.slice(0, 1000)]);
  var excess = sh.getLastRow() - 1 - MAX_LOG_ROWS;
  if (excess > 0) sh.deleteRows(2, excess);
}

// -----------------------------------------------------------------------------
// Supabase HTTP
// -----------------------------------------------------------------------------

function upsertBatch_(cfg, records) {
  request_(cfg, 'post', '/rest/v1/' + TABLE + '?on_conflict=row_id', records, {
    Prefer: 'resolution=merge-duplicates,return=minimal'
  });
}

function reconcileDeletes_(cfg, runId, seenIds) {
  for (var i = 0; i < seenIds.length; i += SEEN_CHUNK) {
    var chunk = seenIds.slice(i, i + SEEN_CHUNK).map(function (id) { return { run_id: runId, row_id: id }; });
    request_(cfg, 'post', '/rest/v1/sync_seen_rows?on_conflict=run_id,row_id', chunk, {
      Prefer: 'resolution=ignore-duplicates,return=minimal'
    });
  }
  var res = request_(cfg, 'post', '/rest/v1/rpc/finalize_sales_sync',
                     { p_run_id: runId, p_min_ratio: cfg.reconcileMinRatio }, {});
  return JSON.parse(res.getContentText() || '{}');
}

/** HTTP with retries on 408/425/429/5xx and network errors (exponential backoff + jitter). */
function request_(cfg, method, path, body, extraHeaders) {
  var headers = { apikey: cfg.key, Accept: 'application/json' };
  // Legacy JWT keys (eyJ...) also go in Authorization; new sb_secret_ keys must NOT.
  if (cfg.key.indexOf('eyJ') === 0) headers.Authorization = 'Bearer ' + cfg.key;
  Object.keys(extraHeaders || {}).forEach(function (k) { headers[k] = extraHeaders[k]; });
  var options = { method: method, headers: headers, muteHttpExceptions: true };
  if (body !== null && body !== undefined) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(body);
  }
  var url = cfg.url + path;
  var lastErr;
  for (var attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      var res = UrlFetchApp.fetch(url, options);
      var code = res.getResponseCode();
      if (code >= 200 && code < 300) return res;
      var text = res.getContentText();
      var retryable = code === 408 || code === 425 || code === 429 || code >= 500;
      lastErr = new Error('Supabase ' + method.toUpperCase() + ' ' + path.split('?')[0] +
                          ' -> HTTP ' + code + ': ' + text.slice(0, 500));
      if (!retryable) throw lastErr;  // 4xx = bad data/config: fail fast with the message
    } catch (e) {
      if (e === lastErr && !isRetryableError_(e)) throw e;
      lastErr = e;
    }
    if (attempt < MAX_ATTEMPTS) {
      var waitMs = Math.min(30000, 1000 * Math.pow(2, attempt - 1)) + Math.floor(Math.random() * 500);
      console.warn('Attempt ' + attempt + ' failed (' + lastErr.message.slice(0, 200) + '); retrying in ' + waitMs + ' ms');
      Utilities.sleep(waitMs);
    }
  }
  throw lastErr;
}

function isRetryableError_(e) {
  return /HTTP (408|425|429|5\d\d)/.test(String(e && e.message));
}

function getConfig_() {
  var p = PropertiesService.getScriptProperties();
  var url = (p.getProperty('SUPABASE_URL') || '').replace(/\/+$/, '');
  var key = p.getProperty('SUPABASE_SERVICE_ROLE_KEY') || p.getProperty('SUPABASE_SECRET_KEY') || '';
  if (!/^https:\/\/.+/.test(url)) throw new Error('Script property SUPABASE_URL is missing or not https.');
  if (!key) throw new Error('Script property SUPABASE_SERVICE_ROLE_KEY is missing.');
  var sheetNames = (p.getProperty('SHEET_NAMES') || 'Sales').split(',')
    .map(function (s) { return s.trim(); }).filter(String);
  var batchSize = parseInt(p.getProperty('BATCH_SIZE') || '500', 10);
  var dateOrder = (p.getProperty('STRING_DATE_ORDER') || 'DMY').toUpperCase();
  if (['DMY', 'MDY', 'YMD'].indexOf(dateOrder) < 0) throw new Error('STRING_DATE_ORDER must be DMY, MDY or YMD.');
  return {
    url: url,
    key: key,
    sheetNames: sheetNames,
    headerRow: parseInt(p.getProperty('HEADER_ROW') || '1', 10),
    batchSize: Math.max(1, Math.min(batchSize, 1000)),
    dateOrder: dateOrder,
    reconcileMinRatio: parseFloat(p.getProperty('RECONCILE_MIN_RATIO') || '0.5')
  };
}
