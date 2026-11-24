# Deploying the inference API

The image is stateless. At startup it downloads the **current production model**
from Supabase Storage (`MODEL_SOURCE=supabase`), verifies its SHA-256 and serves
it. A newly promoted model reaches the API in one of two ways:

- **Hot reload:** the pipeline calls `POST /admin/reload` (needs `ADMIN_TOKEN` on
  the service plus the `API_BASE_URL` and `API_ADMIN_TOKEN` GitHub secrets). No
  downtime.
- **Redeploy:** the pipeline calls the platform's deploy hook, and the new instance
  downloads the new model.

Either way, the workflow then polls `/health` until it reports the new `model_version`.

## Environment variables

| Variable | Required | Example / default | Purpose |
|---|---|---|---|
| `SUPABASE_URL` | yes | `https://abcd.supabase.co` | |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | `sb_secret_…` | store as a **secret** |
| `MODEL_SOURCE` | | `supabase` (image default) | `local` + `MODEL_PATH` to serve a mounted file |
| `MODEL_PATH` | | `artifacts/latest/model.joblib` | only for `MODEL_SOURCE=local` |
| `MODEL_CACHE_DIR` | | `/tmp/model_cache` | downloaded models |
| `TARGET_METRIC` | | `revenue` | must match the trained model |
| `BUSINESS_TZ` | | `Asia/Colombo` | defines "today" |
| `MAX_HORIZON` | | `28` | API limit |
| `STALE_DATA_DAYS` | | `3` | `/predict` returns 503 if the sync is older |
| `HISTORY_CACHE_TTL_S` | | `300` | recent-history cache |
| `API_KEYS` | recommended | `k1,k2` | requires `X-API-Key` on `/predict` |
| `ADMIN_TOKEN` | optional | long random string | enables `/admin/reload` |
| `WEB_CONCURRENCY` | | `1` | uvicorn workers per container |
| `PORT` | | set by platform | |
| `LOG_LEVEL` | | `INFO` | |

Generate tokens with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

> Train and promote the first model **before** the first deploy. Otherwise the
> service correctly refuses to start ("No production model registered").

## Render

1. **New → Web Service** → connect the repo → Runtime **Docker** (it finds the `Dockerfile`).
2. Region: Singapore (closest to Sri Lanka). An instance with ≥ 512 MB RAM is enough for one worker.
3. **Health Check Path:** `/health`.
4. **Environment:** add the variables above (key and tokens as secret values).
5. **Settings → Deploy Hook:** copy the URL into the GitHub secret `RENDER_DEPLOY_HOOK_URL`.
   Also set `API_BASE_URL=https://<service>.onrender.com` so rollouts are verified.
6. Optional hot reload: set `ADMIN_TOKEN` on Render and the same value as `API_ADMIN_TOKEN` in GitHub.

Render sets `PORT`, and the container binds to it automatically.

## Google Cloud Run

```bash
PROJECT=my-project REGION=asia-south1 REPO=sales-forecast
gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com
gcloud artifacts repositories create $REPO --repository-format=docker --location=$REGION
gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT/$REPO/api:$(git rev-parse --short HEAD)

printf '%s' 'sb_secret_xxx' | gcloud secrets create supabase-service-role-key --data-file=-
python -c "import secrets;print(secrets.token_urlsafe(32),end='')" | gcloud secrets create api-admin-token --data-file=-
python -c "import secrets;print(secrets.token_urlsafe(32),end='')" | gcloud secrets create api-keys --data-file=-

gcloud run deploy sales-forecast-api \
  --image $REGION-docker.pkg.dev/$PROJECT/$REPO/api:$(git rev-parse --short HEAD) \
  --region $REGION --memory 1Gi --cpu 1 --min-instances 0 --max-instances 3 \
  --cpu-boost --allow-unauthenticated \
  --set-env-vars SUPABASE_URL=https://abcd.supabase.co,MODEL_SOURCE=supabase,TARGET_METRIC=revenue,BUSINESS_TZ=Asia/Colombo \
  --set-secrets SUPABASE_SERVICE_ROLE_KEY=supabase-service-role-key:latest,ADMIN_TOKEN=api-admin-token:latest,API_KEYS=api-keys:latest
```

- Cloud Run injects `PORT=8080`, and the container honours it.
- For private use, drop `--allow-unauthenticated` and call it with an identity token instead of `API_KEYS`.
- Rollout: Cloud Run has no deploy-hook URL. Use the hot-reload path (`API_BASE_URL` +
  `API_ADMIN_TOKEN` secrets in GitHub). Note that with `--min-instances 0`, every
  new instance already loads the latest production model on cold start.
- `/tmp` is in-memory on Cloud Run; the model is a few MB, so that is fine.

## Railway

1. **New Project → Deploy from GitHub repo**. Railway detects the `Dockerfile`.
2. Variables: as in the table above.
3. Settings → Healthcheck Path: `/health`. Generate a public domain.
4. Rollout: use hot reload (`API_BASE_URL` + `API_ADMIN_TOKEN` GitHub secrets).

## Running the image anywhere

```bash
docker build -t sales-forecast-api .
docker run -d --name forecast-api -p 8000:8000 --env-file .env -e MODEL_SOURCE=supabase sales-forecast-api
# or serve a local model file:
docker run -p 8000:8000 --env-file .env -e MODEL_SOURCE=local \
  -e MODEL_PATH=/models/model.joblib -v "$PWD/artifacts/latest:/models:ro" sales-forecast-api
```

## Rollback

```sql
-- in the Supabase SQL editor: re-promote a previous model
select model_version, status, metrics->>'wape' as wape, trained_at
from model_registry order by trained_at desc limit 10;
select promote_model('revenue-20261117T203012Z-abc1234');
```
Then hot-reload (`curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" $API/admin/reload`)
or restart the service, and re-run the workflow with `forecast-only` to rewrite
`sales_forecasts` with that model.
