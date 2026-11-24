-- =============================================================================
-- Sales Forecasting & ML System — Supabase / PostgreSQL schema
-- =============================================================================
-- Run once in the Supabase SQL editor (or `psql "$SUPABASE_DB_URL" -f schema.sql`).
-- The script is idempotent: re-running it is safe.
--
-- Objects
--   public.sales_transactions   one row per sheet line item (idempotent upsert target)
--   public.sync_seen_rows       staging table used by the nightly delete-reconcile
--   public.model_registry       every trained model + metrics + promotion status
--   public.sales_forecasts      batch forecasts written by ml/predict_batch.py
--   public.v_daily_sales        daily aggregate per (location, category) — the ML input
--   public.v_latest_forecasts   newest forecast per (metric, location, category, date)
--   storage bucket 'ml-models'  private bucket holding model .joblib bundles
--
-- Security model (see bottom of file)
--   * RLS is ON for every table. No policy is granted to `anon`, so the anon key
--     cannot read or write anything here.
--   * The Apps Script sync, the training job, the batch forecaster and the API
--     use the `service_role` key, which bypasses RLS. Keep it server-side only.
--   * `authenticated` users may READ forecasts (for dashboards). Nothing else.
-- =============================================================================

-- CUSTOMIZE: column names below mirror the sheet headers
--   Date | Location | Category | Bill No. | IMEI | Brand/Item | Model | Capacity |
--   Colour | Sales Rep | Revenue | Cost | Profit
-- If you rename a column here, update COLUMN_MAP in apps_script/sync_to_supabase.gs.

begin;

-- -----------------------------------------------------------------------------
-- Helpers
-- -----------------------------------------------------------------------------
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- Only bump updated_at when the business content actually changed. The Apps
-- Script sends a SHA-256 of the business fields as row_hash, so comparing it is
-- cheap and avoids touching generated columns inside a BEFORE trigger.
create or replace function public.touch_sales_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.row_hash is distinct from old.row_hash
     or new.deleted_at is distinct from old.deleted_at then
    new.updated_at := now();
  else
    new.updated_at := old.updated_at;
  end if;
  new.created_at := old.created_at;  -- never let an upsert rewrite created_at
  return new;
end;
$$;

-- -----------------------------------------------------------------------------
-- sales_transactions
-- -----------------------------------------------------------------------------
create table if not exists public.sales_transactions (
  id            bigint generated always as identity primary key,
  -- Stable natural key. "Bill No." is NOT unique per row (one bill has several
  -- line items), so the sync script writes a UUID into a hidden `_row_id`
  -- column in the sheet the first time it sees a row and reuses it forever.
  row_id        text        not null,
  sale_date     date        not null,
  location      text        not null,
  category      text        not null,
  bill_no       text,                       -- optional: some sheet lines have no bill number
  imei          text,                       -- text: preserves leading zeros / multiple IMEIs
  item          text,                       -- sheet: "Brand/Item"
  model         text,
  capacity      text,
  colour        text,
  sales_rep     text,
  revenue       numeric(14, 2) not null,    -- money is NUMERIC, never float
  cost          numeric(14, 2),
  profit        numeric(14, 2) generated always as (revenue - cost) stored,
  source_sheet  text,                       -- which tab the row came from
  row_hash      text        not null,       -- sha256 of business fields (change detection)
  deleted_at    timestamptz,                -- soft delete set by finalize_sales_sync()
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  constraint sales_transactions_row_id_key unique (row_id),
  constraint sales_transactions_location_nonempty check (btrim(location) <> ''),
  constraint sales_transactions_category_nonempty check (btrim(category) <> ''),
  constraint sales_transactions_bill_nonempty check (bill_no is null or btrim(bill_no) <> '')
);

comment on table  public.sales_transactions is 'One row per sales line item, synced idempotently from Google Sheets.';
comment on column public.sales_transactions.row_id is 'UUID stored in the sheet''s hidden _row_id column; upsert conflict target.';
comment on column public.sales_transactions.profit is 'Generated: revenue - cost. NULL when cost is unknown.';

-- Upgrade path for databases created before Bill No. became optional (idempotent).
alter table public.sales_transactions alter column bill_no drop not null;
alter table public.sales_transactions drop constraint if exists sales_transactions_bill_nonempty;
alter table public.sales_transactions
  add constraint sales_transactions_bill_nonempty check (bill_no is null or btrim(bill_no) <> '');

create index if not exists idx_sales_tx_sale_date        on public.sales_transactions (sale_date);
create index if not exists idx_sales_tx_series_date      on public.sales_transactions (location, category, sale_date)
  where deleted_at is null;
create index if not exists idx_sales_tx_bill             on public.sales_transactions (bill_no);
create index if not exists idx_sales_tx_rep_date         on public.sales_transactions (sales_rep, sale_date);
create index if not exists idx_sales_tx_imei             on public.sales_transactions (imei) where imei is not null;

drop trigger if exists trg_sales_tx_updated_at on public.sales_transactions;
create trigger trg_sales_tx_updated_at
  before update on public.sales_transactions
  for each row execute function public.touch_sales_updated_at();

-- -----------------------------------------------------------------------------
-- Delete reconciliation (rows removed from the sheet -> soft-deleted in DB)
-- -----------------------------------------------------------------------------
create table if not exists public.sync_seen_rows (
  run_id     uuid        not null,
  row_id     text        not null,
  created_at timestamptz not null default now(),
  primary key (run_id, row_id)
);

create or replace function public.finalize_sales_sync(
  p_run_id    uuid,
  p_min_ratio numeric default 0.5
)
returns jsonb
language plpgsql
set search_path = public
as $$
declare
  v_seen     integer;
  v_active   integer;
  v_deleted  integer;
  v_restored integer;
begin
  -- Garbage-collect staging rows left by aborted runs.
  delete from sync_seen_rows where created_at < now() - interval '1 day';

  select count(*) into v_seen   from sync_seen_rows      where run_id = p_run_id;
  select count(*) into v_active from sales_transactions  where deleted_at is null;

  -- Safety valve: if the sheet suddenly looks much smaller than the DB (a tab was
  -- renamed, a filter view was active, the sheet was emptied by mistake), refuse
  -- to soft-delete. The exception rolls the whole call back.
  if v_active > 0 and v_seen < v_active * p_min_ratio then
    raise exception 'finalize_sales_sync aborted: run % saw % rows but % rows are active (min ratio %)',
      p_run_id, v_seen, v_active, p_min_ratio;
  end if;

  update sales_transactions t
     set deleted_at = now()
   where t.deleted_at is null
     and not exists (select 1 from sync_seen_rows s where s.run_id = p_run_id and s.row_id = t.row_id);
  get diagnostics v_deleted = row_count;

  update sales_transactions t
     set deleted_at = null
   where t.deleted_at is not null
     and exists (select 1 from sync_seen_rows s where s.run_id = p_run_id and s.row_id = t.row_id);
  get diagnostics v_restored = row_count;

  delete from sync_seen_rows where run_id = p_run_id;

  return jsonb_build_object(
    'run_id', p_run_id, 'seen', v_seen, 'active_before', v_active,
    'soft_deleted', v_deleted, 'restored', v_restored
  );
end;
$$;

-- -----------------------------------------------------------------------------
-- model_registry
-- -----------------------------------------------------------------------------
create table if not exists public.model_registry (
  model_version    text primary key,                 -- e.g. revenue-20261124T013000Z-ab12cd3
  target_metric    text not null check (target_metric in ('revenue', 'profit', 'units')),
  status           text not null default 'candidate'
                   check (status in ('candidate', 'production', 'rejected', 'archived')),
  artifact_path    text not null,                    -- object path inside the ml-models bucket
  artifact_sha256  text not null,                    -- verified before every load
  metrics          jsonb not null,                   -- holdout metrics of the candidate
  baseline_metrics jsonb,                            -- seasonal-naive baseline on same holdout
  params           jsonb,
  feature_columns  jsonb,
  train_rows       integer,
  n_series         integer,
  data_start       date,
  data_end         date,
  git_sha          text,
  decision_reason  text,
  trained_at       timestamptz not null,
  promoted_at      timestamptz,
  created_at       timestamptz not null default now()
);

-- Exactly one production model per target metric.
create unique index if not exists uq_model_registry_one_production
  on public.model_registry (target_metric) where status = 'production';

create or replace function public.promote_model(p_model_version text)
returns void
language plpgsql
set search_path = public
as $$
declare
  v_target text;
begin
  select target_metric into v_target
    from model_registry where model_version = p_model_version
     for update;
  if not found then
    raise exception 'promote_model: unknown model_version %', p_model_version;
  end if;

  update model_registry
     set status = 'archived'
   where target_metric = v_target and status = 'production' and model_version <> p_model_version;

  update model_registry
     set status = 'production', promoted_at = now()
   where model_version = p_model_version;
end;
$$;

-- -----------------------------------------------------------------------------
-- sales_forecasts
-- -----------------------------------------------------------------------------
create table if not exists public.sales_forecasts (
  id               bigint generated always as identity primary key,
  forecast_date    date           not null,
  location         text           not null,
  category         text           not null,
  entity_id        text           generated always as (location || '|' || category) stored,
  target_metric    text           not null check (target_metric in ('revenue', 'profit', 'units')),
  predicted_value  numeric(14, 2) not null,
  model_version    text           not null references public.model_registry (model_version),
  run_id           uuid           not null,
  generated_at     timestamptz    not null default now(),
  created_at       timestamptz    not null default now(),
  updated_at       timestamptz    not null default now(),
  constraint sales_forecasts_natural_key
    unique (model_version, target_metric, location, category, forecast_date)
);

create index if not exists idx_sales_forecasts_date   on public.sales_forecasts (forecast_date);
create index if not exists idx_sales_forecasts_entity on public.sales_forecasts (entity_id, forecast_date);
create index if not exists idx_sales_forecasts_latest
  on public.sales_forecasts (target_metric, location, category, forecast_date, generated_at desc);

drop trigger if exists trg_sales_forecasts_updated_at on public.sales_forecasts;
create trigger trg_sales_forecasts_updated_at
  before update on public.sales_forecasts
  for each row execute function public.touch_updated_at();

-- -----------------------------------------------------------------------------
-- Views (security_invoker => the caller's RLS applies, not the view owner's)
-- -----------------------------------------------------------------------------
-- NOTE: there is no quantity column in the sheet, so `units` = number of line items.
create or replace view public.v_daily_sales
with (security_invoker = true) as
select
  sale_date,
  location,
  category,
  sum(revenue)::numeric(14, 2)        as revenue,
  sum(coalesce(profit, 0))::numeric(14, 2) as profit,  -- rows with unknown cost contribute 0
  count(*)::integer                   as units,
  count(distinct bill_no)::integer    as bills
from public.sales_transactions
where deleted_at is null
group by sale_date, location, category;

create or replace view public.v_latest_forecasts
with (security_invoker = true) as
select distinct on (target_metric, location, category, forecast_date)
  forecast_date, location, category, entity_id, target_metric,
  predicted_value, model_version, generated_at
from public.sales_forecasts
order by target_metric, location, category, forecast_date, generated_at desc;

-- -----------------------------------------------------------------------------
-- Row Level Security & grants
-- -----------------------------------------------------------------------------
alter table public.sales_transactions enable row level security;
alter table public.sync_seen_rows     enable row level security;
alter table public.model_registry     enable row level security;
alter table public.sales_forecasts    enable row level security;

-- No policies on sales_transactions / sync_seen_rows / model_registry:
-- anon + authenticated get nothing; service_role bypasses RLS.

drop policy if exists "authenticated users can read forecasts" on public.sales_forecasts;
create policy "authenticated users can read forecasts"
  on public.sales_forecasts for select
  to authenticated
  using (true);

-- OPTIONAL: public read-only forecasts for an unauthenticated dashboard.
-- Only enable if forecasts are not commercially sensitive.
-- create policy "anon can read forecasts" on public.sales_forecasts
--   for select to anon using (true);

-- Supabase grants table privileges to anon/authenticated by default; RLS already
-- blocks them, but revoking makes the intent explicit and protects the views.
revoke all on public.sales_transactions, public.sync_seen_rows, public.model_registry
  from anon, authenticated;
revoke insert, update, delete, truncate on public.sales_forecasts from anon, authenticated;
revoke all on public.v_daily_sales from anon, authenticated;
revoke all on public.v_latest_forecasts from anon;
grant select on public.sales_forecasts, public.v_latest_forecasts to authenticated;

-- Functions in `public` are executable by everyone by default and are exposed as
-- /rest/v1/rpc/<name>. Lock the write-capable ones to service_role.
revoke execute on function public.finalize_sales_sync(uuid, numeric) from public, anon, authenticated;
revoke execute on function public.promote_model(text)                from public, anon, authenticated;
grant  execute on function public.finalize_sales_sync(uuid, numeric) to service_role;
grant  execute on function public.promote_model(text)                to service_role;

grant all on public.sales_transactions, public.sync_seen_rows, public.model_registry,
             public.sales_forecasts to service_role;
grant select on public.v_daily_sales, public.v_latest_forecasts to service_role;

-- -----------------------------------------------------------------------------
-- Dashboard (dashboard/index.html, hosted on GitHub Pages)
-- -----------------------------------------------------------------------------
-- The page signs in with Supabase Auth and the PUBLISHABLE key, then calls
-- dashboard_data(). Only emails listed in dashboard_viewers get data, even if
-- someone else manages to create an account. Add viewers with:
--   insert into public.dashboard_viewers (email) values ('you@example.com');
create table if not exists public.dashboard_viewers (
  email      text primary key check (email = lower(btrim(email)) and email like '%@%'),
  created_at timestamptz not null default now()
);
alter table public.dashboard_viewers enable row level security;  -- no policies: invisible to API users

create or replace function public.is_dashboard_viewer()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from public.dashboard_viewers
     where email = lower(btrim(coalesce(auth.jwt() ->> 'email', '')))
  );
$$;

create or replace function public.dashboard_data(p_days_back integer default 120)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_today  date := (now() at time zone 'Asia/Colombo')::date;  -- CUSTOMIZE: business time zone
  v_from   date := v_today - least(greatest(coalesce(p_days_back, 120), 14), 730);
  v_target text;
begin
  if not public.is_dashboard_viewer() then
    raise exception 'This account is not allowed to view the dashboard' using errcode = '42501';
  end if;

  select target_metric into v_target
    from model_registry where status = 'production'
   order by promoted_at desc nulls last limit 1;
  v_target := coalesce(v_target, 'revenue');

  return jsonb_build_object(
    'today', v_today,
    'target_metric', v_target,
    'last_sync', (select max(updated_at) from sales_transactions),
    'latest_sale_date', (select max(sale_date) from sales_transactions where deleted_at is null),
    'model', (
      select jsonb_build_object(
        'model_version', model_version, 'trained_at', trained_at, 'promoted_at', promoted_at,
        'data_end', data_end, 'wape', metrics -> 'wape', 'baseline_wape', baseline_metrics -> 'wape')
        from model_registry where status = 'production' and target_metric = v_target
       order by promoted_at desc nulls last limit 1),
    'actuals', coalesce((
      select jsonb_agg(jsonb_build_object(
               'd', sale_date, 'location', location, 'category', category,
               'value', case v_target when 'profit' then profit when 'units' then units else revenue end)
             order by sale_date, location, category)
        from v_daily_sales where sale_date >= v_from), '[]'::jsonb),
    'forecasts', coalesce((
      select jsonb_agg(jsonb_build_object(
               'd', forecast_date, 'location', location, 'category', category,
               'value', predicted_value, 'generated_at', generated_at)
             order by forecast_date, location, category)
        from v_latest_forecasts
       where target_metric = v_target and forecast_date >= v_from), '[]'::jsonb)
  );
end;
$$;

revoke all on public.dashboard_viewers from anon, authenticated;
revoke execute on function public.is_dashboard_viewer()          from public, anon;
revoke execute on function public.dashboard_data(integer)        from public, anon;
grant  execute on function public.is_dashboard_viewer()          to authenticated;
grant  execute on function public.dashboard_data(integer)        to authenticated;

-- -----------------------------------------------------------------------------
-- Storage bucket for model artifacts (private; service_role bypasses storage RLS)
-- -----------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('ml-models', 'ml-models', false)
on conflict (id) do nothing;

commit;
