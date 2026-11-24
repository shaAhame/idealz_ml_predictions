# Verification checklist

Work through these in order. Each step says what "good" looks like.

## 0. Offline (no cloud accounts)

- [ ] `pip install -r requirements-dev.txt && pytest`: **46 passed**. This covers
      feature leakage, train/serve consistency, the client's pagination, retries and
      idempotent upserts, the gate logic, the API contract, and a full emulated
      train → promote → forecast → serve run.
- [ ] `node tests/apps_script/sync.test.js`: **10 Apps Script tests passed**
      (idempotent re-runs, stable ids after sorting, copied rows, invalid rows,
      retries, delete reconcile, multiple tabs).
- [ ] Schema on plain Postgres (optional, needs `psql`):
      ```bash
      psql "$DB" -f supabase/tests/supabase_stub.sql -f supabase/schema.sql -f supabase/schema.sql \
           -f supabase/tests/schema_behaviour_test.sql   # → ALL SCHEMA BEHAVIOUR TESTS PASSED
      ```

## 1. Database upsert & idempotency (Supabase)

- [ ] `schema.sql` ran without errors; re-running it also succeeds.
- [ ] Apps Script `testConnection` logs `Connection OK`.
- [ ] Run `syncSales`. In the SQL editor:
      ```sql
      select count(*), count(distinct row_id), min(sale_date), max(sale_date) from sales_transactions;
      ```
      `count = count(distinct row_id)` = number of non-blank sheet rows (minus rows listed in `_sync_errors`).
- [ ] **Idempotency:** note `max(updated_at)`, run `syncSales` again. `_sync_log`
      shows `Upserted = 0`, `Unchanged = N`; row count and `max(updated_at)` are unchanged.
- [ ] **Edit propagation:** change one Revenue cell, run `syncSales`. `_sync_log` shows
      `Upserted = 1`, and the row's `revenue`, `profit` and `updated_at` change:
      ```sql
      select row_id, revenue, cost, profit, updated_at from sales_transactions order by updated_at desc limit 3;
      ```
- [ ] **Force full re-send:** menu → *Force full re-send on next sync*, then *Sync now*. Row count is unchanged.
- [ ] **Deletes:** delete a test row in the sheet, run `reconcileSales`. `_sync_log`
      message shows `"soft_deleted":1` and:
      ```sql
      select count(*) from sales_transactions where deleted_at is not null;
      ```
- [ ] **RLS:** with the *publishable/anon* key this must return `[]` or 401/permission denied, never data:
      ```bash
      curl -s "$SUPABASE_URL/rest/v1/sales_transactions?select=*&limit=1" -H "apikey: $SUPABASE_PUBLISHABLE_KEY"
      ```
- [ ] Daily view looks right: `select * from v_daily_sales order by sale_date desc limit 20;`

## 2. Training locally against Supabase

```bash
cp .env.example .env   # fill SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY, DATA_SOURCE=supabase
python -m ml.train --no-upload              # dry run: nothing written to Supabase
cat artifacts/latest/metadata.json | python -m json.tool | head -40
```
- [ ] The log line `training finished` shows `wape` < `baseline_wape`.
- [ ] `metadata.json` has `metrics.by_horizon`, `metrics.by_category`, `artifact_sha256`.
- [ ] Real run: `python -m ml.train`, then
      ```sql
      select model_version, status, metrics->>'wape' wape, baseline_metrics->>'wape' base, decision_reason
      from model_registry order by trained_at desc;
      ```
      One row is `production` (or `rejected` with a reason). Storage → `ml-models`
      → `models/revenue/<version>/` holds `model.joblib` and `metadata.json`.

## 3. Batch forecasts → `sales_forecasts`

```bash
python -m ml.predict_batch --dry-run --out forecasts.csv && head forecasts.csv   # nothing written
python -m ml.predict_batch                                                       # writes
python -m ml.predict_batch                                                       # again: idempotent
```
```sql
select model_version, count(*), min(forecast_date), max(forecast_date)
from sales_forecasts group by 1 order by 1 desc;
-- count = active series × FORECAST_HORIZON, unchanged after the second run
select * from v_latest_forecasts where forecast_date >= current_date order by entity_id, forecast_date limit 20;
```

## 4. API

```bash
MODEL_SOURCE=supabase uvicorn api.main:app --port 8000      # or docker run ... (see DEPLOYMENT.md)
curl -s localhost:8000/health | python -m json.tool          # status ok, model_version = production
curl -s -X POST localhost:8000/predict -H 'content-type: application/json' \
  -d '{"series":[{"location":"ISEVEN","category":"ACCESSORIES"}],"horizon":7}' | python -m json.tool
curl -s -X POST localhost:8000/predict -H 'content-type: application/json' -d '{"horizon":3}'   # all active series
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/predict \
  -H 'content-type: application/json' -d '{"horizon":999}'  # 422
```
- [ ] The `/predict` values for the next days match `v_latest_forecasts` when the
      batch job ran today with the same model.
- [ ] With `API_KEYS` set: a request without `X-API-Key` returns 401.
- [ ] Fail-fast: `MODEL_SOURCE=local MODEL_PATH=/nope uvicorn api.main:app` exits
      non-zero with the log `could not load model at startup`.

## 5. CI/CD

- [ ] Pushing to `main` turns **ci** green (python, apps-script, schema, docker).
- [ ] Actions → **ml-pipeline** → Run workflow → `train-and-forecast`: the job summary
      shows the metrics table and the `Promoted:` line; the `deploy` job verifies
      `/health` reports the new version.
- [ ] Run with `promote = never`: registry gets a `candidate` and production is unchanged.
- [ ] Next morning: the scheduled `forecast` job ran and `max(generated_at)` in
      `sales_forecasts` is from today.

## 6. Operational alarms to set up

- GitHub: notifications for failed workflow runs (training/forecast failures, stale data).
- Apps Script: Triggers page → failure notifications → *daily* (or *immediately*).
- Platform uptime check on `/health`.
