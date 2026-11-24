-- Behavioural checks for schema.sql. Run against a scratch database:
--   psql "$DB_URL" -v ON_ERROR_STOP=1 -f supabase/tests/schema_behaviour_test.sql
-- Everything runs in a transaction that is rolled back at the end.
begin;

-- 1. Idempotent upsert (same statement PostgREST issues for merge-duplicates)
insert into sales_transactions (row_id, sale_date, location, category, bill_no, item, sales_rep, revenue, cost, row_hash)
values ('r1','2026-07-01','ISEVEN','ACCESSORIES','1604','GREEN 4 IN 1','AADHIL',10000,7500,'h1'),
       ('r2','2026-07-01','ISEVEN','ACCESSORIES','1604','BACKCASE','AADHIL',3000,2000,'h2')
on conflict (row_id) do update set sale_date=excluded.sale_date, revenue=excluded.revenue, cost=excluded.cost, row_hash=excluded.row_hash;
-- backdate updated_at (trigger disabled) so we can observe whether it moves
alter table sales_transactions disable trigger trg_sales_tx_updated_at;
update sales_transactions set updated_at = '2000-01-01';
alter table sales_transactions enable trigger trg_sales_tx_updated_at;
insert into sales_transactions (row_id, sale_date, location, category, bill_no, item, sales_rep, revenue, cost, row_hash)
values ('r1','2026-07-01','ISEVEN','ACCESSORIES','1604','GREEN 4 IN 1','AADHIL',10000,7500,'h1')
on conflict (row_id) do update set revenue=excluded.revenue, cost=excluded.cost, row_hash=excluded.row_hash;
do $$ begin
  assert (select count(*) from sales_transactions) = 2, 'upsert re-run must not duplicate';
  assert (select profit from sales_transactions where row_id='r1') = 2500, 'generated profit';
  assert (select updated_at = '2000-01-01'::timestamptz from sales_transactions where row_id='r1'), 'unchanged hash must not bump updated_at';
end $$;

-- 1b. Bill No. is optional
insert into sales_transactions (row_id, sale_date, location, category, bill_no, revenue, row_hash)
values ('r0','2026-06-30','ISEVEN','ACCESSORIES',null,500,'h0');
do $$ begin assert (select bills from v_daily_sales where sale_date='2026-06-30') = 0; end $$;
delete from sales_transactions where row_id='r0';

-- 2. Changed content bumps updated_at
insert into sales_transactions (row_id, sale_date, location, category, bill_no, revenue, cost, row_hash)
values ('r1','2026-07-01','ISEVEN','ACCESSORIES','1604',11000,7500,'h1b')
on conflict (row_id) do update set revenue=excluded.revenue, row_hash=excluded.row_hash;
do $$ begin
  assert (select updated_at > '2000-01-01'::timestamptz from sales_transactions where row_id='r1'), 'changed hash must bump updated_at';
  assert (select revenue from v_daily_sales where sale_date='2026-07-01') = 14000, 'view aggregates';
end $$;

-- 3. Reconcile: r2 missing from the sheet -> soft-deleted; ratio guard
insert into sync_seen_rows(run_id,row_id) values ('00000000-0000-0000-0000-000000000001','r1');
select finalize_sales_sync('00000000-0000-0000-0000-000000000001', 0.5);
do $$ begin
  assert (select deleted_at is not null from sales_transactions where row_id='r2'), 'r2 soft-deleted';
  assert (select units from v_daily_sales where sale_date='2026-07-01') = 1, 'view hides deleted';
  assert (select count(*) from sync_seen_rows) = 0, 'staging cleaned';
end $$;
-- restore
insert into sync_seen_rows(run_id,row_id) values ('00000000-0000-0000-0000-000000000002','r1'),('00000000-0000-0000-0000-000000000002','r2');
select finalize_sales_sync('00000000-0000-0000-0000-000000000002', 0.5);
do $$ begin assert (select deleted_at is null from sales_transactions where row_id='r2'), 'r2 restored'; end $$;
-- guard: a run that saw nothing must abort
savepoint guard;
do $$ begin
  perform finalize_sales_sync('00000000-0000-0000-0000-000000000003', 0.5);
  raise exception 'guard did not fire';
exception when others then
  if sqlerrm like 'guard did not fire' then raise; end if;
end $$;
rollback to savepoint guard;

-- 4. Registry promotion keeps exactly one production model per target
insert into model_registry(model_version,target_metric,artifact_path,artifact_sha256,metrics,trained_at)
values ('v1','revenue','p1','s1','{}',now()),('v2','revenue','p2','s2','{}',now());
select promote_model('v1'); select promote_model('v2');
do $$ begin
  assert (select status from model_registry where model_version='v1') = 'archived';
  assert (select status from model_registry where model_version='v2') = 'production';
end $$;

-- 5. Forecast upsert + latest view
insert into sales_forecasts(forecast_date,location,category,target_metric,predicted_value,model_version,run_id)
values ('2026-07-02','ISEVEN','ACCESSORIES','revenue',123.45,'v2',gen_random_uuid())
on conflict on constraint sales_forecasts_natural_key do update set predicted_value=excluded.predicted_value;
insert into sales_forecasts(forecast_date,location,category,target_metric,predicted_value,model_version,run_id)
values ('2026-07-02','ISEVEN','ACCESSORIES','revenue',200,'v2',gen_random_uuid())
on conflict on constraint sales_forecasts_natural_key do update set predicted_value=excluded.predicted_value;
do $$ begin
  assert (select count(*) from sales_forecasts) = 1;
  assert (select predicted_value from v_latest_forecasts) = 200;
  assert (select entity_id from sales_forecasts) = 'ISEVEN|ACCESSORIES';
end $$;

-- 6. RLS: anon sees nothing, cannot call RPCs; authenticated reads forecasts only
set local role anon;
do $$ begin
  begin perform 1 from sales_transactions; raise exception 'anon read tx'; exception when insufficient_privilege then null; end;
  begin perform promote_model('v1'); raise exception 'anon promoted'; exception when insufficient_privilege then null; end;
end $$;
reset role;
set local role authenticated;
do $$ begin
  assert (select count(*) from sales_forecasts) = 1, 'authenticated reads forecasts';
  begin perform 1 from v_daily_sales; raise exception 'auth read daily view'; exception when insufficient_privilege then null; end;
  begin perform 1 from sales_transactions; raise exception 'auth read tx'; exception when insufficient_privilege then null; end;
end $$;
reset role;

-- 7. Dashboard: only allow-listed emails get data; anon cannot call it at all
insert into dashboard_viewers(email) values ('owner@shop.lk');
set local role authenticated;
select set_config('request.jwt.claims', '{"email":"Owner@Shop.lk","role":"authenticated"}', true);
do $$
declare d jsonb;
begin
  d := dashboard_data(30);
  assert d->>'target_metric' = 'revenue', 'target';
  assert jsonb_typeof(d->'actuals') = 'array' and jsonb_typeof(d->'forecasts') = 'array', 'arrays';
  assert (d->'model'->>'model_version') = 'v2', 'production model';
end $$;
select set_config('request.jwt.claims', '{"email":"stranger@example.com","role":"authenticated"}', true);
do $$ begin
  begin perform dashboard_data(30); raise exception 'stranger got data'; exception when insufficient_privilege then null; end;
  begin perform 1 from dashboard_viewers; raise exception 'viewer list readable'; exception when insufficient_privilege then null; end;
end $$;
reset role;
set local role anon;
do $$ begin
  begin perform dashboard_data(30); raise exception 'anon got data'; exception when insufficient_privilege then null; end;
end $$;
reset role;

select 'ALL SCHEMA BEHAVIOUR TESTS PASSED' as result;
rollback;
