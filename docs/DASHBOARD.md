# Sales forecast dashboard

A phone-friendly page with your forecast vs actual sales, hosted free on GitHub Pages:

**https://shaahame.github.io/idealz_ml_predictions/**

## How it stays private

- The page holds **no data and no secret key**. It contains only your Supabase URL
  and the **publishable** key, which Supabase designs to be public.
- After you sign in (Supabase Auth, email + password), the page calls one database
  function, `dashboard_data()`. That function returns data **only** if your email is
  in the `dashboard_viewers` table. Anyone else, even with an account, gets
  "not on the viewer list".
- The deploy workflow refuses to publish if a secret key is put in the wrong place, and
  reads only GitHub *secrets*, so a mix-up can't print a key in the public run logs.

## One-time setup

1. **Database:** in Supabase **SQL Editor**, run the latest `supabase/schema.sql`
   (safe to re-run), then add yourself as a viewer:
   ```sql
   insert into public.dashboard_viewers (email) values ('you@example.com');
   ```
2. **Login account:** Supabase → **Authentication → Users → Add user → Create new user**.
   Enter the same email and a strong password, and tick **Auto Confirm User**.
3. **Block strangers from signing up (recommended):** Supabase → **Authentication →
   Sign In / Providers** → turn off **Allow new users to sign up**.
4. **Publishable key:** Supabase → **Project Settings → API Keys** → copy the
   **Publishable key** (`sb_publishable_…`), **not** the secret key.
5. **GitHub secrets:** repo → **Settings → Secrets and variables → Actions →
   Secrets** tab → **New repository secret** (secrets, not variables: GitHub hides
   secrets in its public logs):
   - `SUPABASE_URL` = `https://<project-ref>.supabase.co`
   - `SUPABASE_PUBLISHABLE_KEY` = the publishable key
6. **Turn on Pages:** repo → **Settings → Pages** → *Build and deployment* → **Source:
   GitHub Actions**.
7. **Publish:** **Actions → dashboard → Run workflow**. After about 1 minute, the
   site is live at the address above.
8. **On your phone:** open the address, sign in, then use **Share → Add to Home
   Screen** (iPhone) or **⋮ → Add to Home screen** (Android) to get an app icon.

## Adding another viewer

```sql
insert into public.dashboard_viewers (email) values ('manager@example.com');
```
Then create their login under **Authentication → Users → Add user**.
To remove someone: `delete from public.dashboard_viewers where email = 'manager@example.com';`

## What it shows

- **Forecast next 7 / 14 days** and **actual last 7 days** (with change vs the week before).
- **Forecast accuracy:** the model's typical daily error per category, and how much
  better it is than "same as last week".
- **Daily chart:** actual sales (solid) for 8 weeks, forecast (dashed) for the next
  14 days. As days pass, the dashed line also shows what was forecast for past days,
  so you can compare. Tap or hover for values.
- **Next 7 days by category** and a **day-by-day table** next to the same weekday last week.
- **Location / Category filters** (remembered on the device).

A warning appears if no new sales have arrived for 3+ days, which means the sheet sync needs attention.
