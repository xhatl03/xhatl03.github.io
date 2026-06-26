# Ostrava BFL festival weather — ECMWF ensemble nowcast

Twice-daily ensemble forecast for **Beats for Love** in Ostrava (49.81 N, 18.28 E),
covering the festival window **čt 2. 7. 2026 14:00 → ne 5. 7. 2026 03:00** (local,
Europe/Prague).

## What `forecast.py` does

1. Fetches the **ECMWF IFS 0.25°** ensemble (`models=ecmwf_ifs025`) from
   Open-Meteo's ensemble API — hourly `precipitation`, `temperature_2m`,
   `wind_speed_10m`, `wind_gusts_10m`, 10 forecast days.
2. Across the ensemble members computes the **10/25/50/75/90 percentiles** in
   **6-hour buckets** over the festival window.
3. Festival-aware read: rain during play hours (afternoon/evening/night),
   daytime temperatures **and the overnight comfort minimum**, wind gusts.
4. Diffs against the previous run in `last.json` (rain-timing shift, cooling /
   warming, gust change) and overwrites `last.json`.
5. Writes a ≤5-sentence Czech summary to `email_body.txt` and stdout:
   *vedro / déšť / vítr / změna vs. minule / spolehlivost*.

State (`last.json`, `email_body.txt`, `raw_latest.json`) is written next to the
script. The live runtime keeps it in `~/ostrava-bfl/`.

## Network requirement

The Open-Meteo ensemble host (`ensemble-api.open-meteo.com`) must be reachable.
In a restricted environment it must be **allow-listed in the egress/network
policy**, otherwise the fetch returns HTTP 403. See
<https://code.claude.com/docs/en/claude-code-on-the-web>.

## Run

```bash
python3 forecast.py                 # live fetch + analyse + diff + email body
python3 forecast.py --offline=raw.json   # replay a saved ensemble JSON (testing)
```

## Twice-daily automation — GitHub Actions (durable)

`.github/workflows/ostrava-bfl-weather.yml` runs the pipeline every 12 h on a
GitHub-hosted runner (no egress restrictions, so no Open-Meteo 403) and emails
the summary over **SMTP**. `last.json` is carried between runs via a rolling
`actions/cache`, so the run-to-run diff works.

**Two things to do before it works:**

1. **Schedule only fires from the default branch.** Merge this branch into
   `main`; until then trigger it manually via *Actions → Ostrava BFL festival
   weather → Run workflow*.
2. **Add repository secrets** (*Settings → Secrets and variables → Actions*):

   | Secret | Example | Notes |
   |---|---|---|
   | `SMTP_HOST` | `smtp.gmail.com` | required |
   | `SMTP_PORT` | `465` (SSL) or `587` (STARTTLS) | default 587 |
   | `SMTP_USER` | `hatle.lukas@gmail.com` | required for auth |
   | `SMTP_PASS` | *app password* | for Gmail use an **App Password**, not your login |
   | `MAIL_FROM` | `hatle.lukas@gmail.com` | defaults to `SMTP_USER` |
   | `MAIL_TO` | `hatle.lukas@gmail.com` | defaults to `hatle.lukas@gmail.com` |

   Without `SMTP_HOST`/`MAIL_TO` the run still computes everything and uploads
   `email_body.txt` as an artifact — it just doesn't send.

## Twice-daily automation (in-session runbook prompt)

Run every 12 h. Each run: execute `python3 ~/ostrava-bfl/forecast.py`; on success
create a Gmail **draft** to `hatle.lukas@gmail.com` (no send tool is available in
the hosted Gmail integration) with the contents of `email_body.txt`, subject
`Ostrava BFL počasí — <datum>`, and report the 5 sentences in chat. If the fetch
fails with 403, the host is not yet allow-listed — report that and create no draft.

> The ECMWF ensemble (`ecmwf_ifs025`) only covers the festival window once it
> enters the ~10-day forecast range, i.e. from **~22. 6. 2026** onward.
