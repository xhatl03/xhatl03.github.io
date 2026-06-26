#!/usr/bin/env python3
"""
Ostrava Beats for Love (BFL) festival weather — ECMWF ensemble nowcast.

Pipeline:
 1. Fetch the ECMWF IFS 0.25deg ensemble (model id ``ecmwf_ifs025``) from
    Open-Meteo for Ostrava (49.81N, 18.28E), 10 forecast days, hourly
    precipitation / temperature_2m / wind_speed_10m (+ wind_gusts_10m for gust
    analysis).
 2. Across the ensemble members compute the 10/25/50/75/90 percentiles in 6h
    buckets over the festival window (local time, Europe/Prague):
        Thu 2026-07-02 14:00  ->  Sun 2026-07-05 03:00
 3. Festival-aware summary: rain during play hours (afternoon/evening/night),
    temperature incl. the overnight comfort minimum, wind gusts.
 4. Diff against the previous run in ``last.json`` (rain timing shift, cooling)
    and overwrite ``last.json`` with the new run.
 5. Emit a short (<=5 sentence) Czech e-mail body to stdout / email_body.txt:
        heat / rain / wind / change-vs-last / reliability.

The Open-Meteo call honours the standard HTTPS_PROXY / CA-bundle env so it works
behind the agent egress proxy once the host is allow-listed.
"""

import json
import os
import smtplib
import ssl
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formatdate

# ---------------------------------------------------------------- config ----
LAT, LON = 49.81, 18.28
MODEL = "ecmwf_ifs025"
TZ = "Europe/Prague"
FORECAST_DAYS = 10
HOURLY_VARS = ["precipitation", "temperature_2m", "wind_speed_10m", "wind_gusts_10m"]
PERCENTILES = [10, 25, 50, 75, 90]

HERE = os.path.dirname(os.path.abspath(__file__))
LAST_PATH = os.path.join(HERE, "last.json")
BODY_PATH = os.path.join(HERE, "email_body.txt")
RAW_PATH = os.path.join(HERE, "raw_latest.json")

# Festival window in LOCAL time (CEST = UTC+2 in July). Naive = local.
WIN_START = datetime(2026, 7, 2, 14, 0)
WIN_END = datetime(2026, 7, 5, 3, 0)

CZ_DAY = {0: "po", 1: "út", 2: "st", 3: "čt", 4: "pá", 5: "so", 6: "ne"}


# ----------------------------------------------------------------- fetch ----
def fetch(offline_path=None):
    """Return the Open-Meteo ensemble JSON. If offline_path is given, load it
    from disk instead of the network (used for testing / replay)."""
    if offline_path:
        with open(offline_path) as fh:
            return json.load(fh)
    qs = urllib.parse.urlencode({
        "latitude": LAT,
        "longitude": LON,
        "hourly": ",".join(HOURLY_VARS),
        "models": MODEL,
        "forecast_days": FORECAST_DAYS,
        "timezone": TZ,
    })
    url = "https://ensemble-api.open-meteo.com/v1/ensemble?" + qs
    ca = os.environ.get("CURL_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    # urllib reads HTTPS_PROXY from the environment via ProxyHandler defaults.
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90, context=ctx) as resp:
        data = json.load(resp)
    with open(RAW_PATH, "w") as fh:
        json.dump(data, fh)
    return data


# ------------------------------------------------------------ structuring ----
def member_series(hourly, var):
    """Collect every ensemble member's hourly list for one variable.
    Open-Meteo names them ``<var>`` (control) and ``<var>_memberNN``."""
    series = []
    if var in hourly and hourly[var] is not None:
        series.append(hourly[var])
    i = 1
    while True:
        key = f"{var}_member{i:02d}"
        if key in hourly and hourly[key] is not None:
            series.append(hourly[key])
            i += 1
        else:
            break
    return series


def parse_times(hourly):
    """Open-Meteo time strings are local (tz applied). Parse to naive local."""
    return [datetime.strptime(t, "%Y-%m-%dT%H:%M") for t in hourly["time"]]


def percentile(sorted_vals, p):
    """Linear-interpolation percentile over an already-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def buckets_6h(start, end):
    """6h buckets from start; the final bucket is clipped to end."""
    out = []
    b = start
    while b < end:
        out.append((b, min(b + timedelta(hours=6), end)))
        b += timedelta(hours=6)
    return out


def daypart(hour):
    if 6 <= hour < 12:
        return "ráno"
    if 12 <= hour < 18:
        return "odpoledne"
    if 18 <= hour < 24:
        return "večer"
    return "noc"


def bucket_label(b0, b1):
    return f"{CZ_DAY[b0.weekday()]} {b0:%H:%M}–{b1:%H:%M} ({daypart(b0.hour)})"


def aggregate(values, how):
    """Aggregate a member's hourly values within a bucket."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    if how == "sum":
        return sum(vals)
    if how == "max":
        return max(vals)
    if how == "min":
        return min(vals)
    return sum(vals) / len(vals)  # mean


def analyse(data):
    hourly = data["hourly"]
    times = parse_times(hourly)
    series = {v: member_series(hourly, v) for v in HOURLY_VARS}
    n_members = max((len(series[v]) for v in HOURLY_VARS), default=0)

    # how to aggregate each variable inside a 6h bucket, before taking
    # cross-member percentiles
    agg = {
        "precipitation": "sum",   # total rain in the 6h
        "temperature_2m": "mean",
        "wind_speed_10m": "max",
        "wind_gusts_10m": "max",
    }

    out_buckets = []
    night_min_members = []  # per member: min temperature over overnight hours

    for (b0, b1) in buckets_6h(WIN_START, WIN_END):
        idx = [i for i, t in enumerate(times) if b0 <= t < b1]
        rec = {"label": bucket_label(b0, b1),
               "start": b0.isoformat(), "end": b1.isoformat(),
               "daypart": daypart(b0.hour), "hours": len(idx)}
        for var in HOURLY_VARS:
            per_member = []
            for ms in series[var]:
                per_member.append(aggregate([ms[i] for i in idx], agg[var]))
            per_member = sorted(v for v in per_member if v is not None)
            rec[var] = {f"p{p}": (round(percentile(per_member, p), 1)
                                  if per_member else None)
                        for p in PERCENTILES}
        out_buckets.append(rec)

    # overnight comfort minimum: min temperature per member over night hours
    night_idx = [i for i, t in enumerate(times)
                 if WIN_START <= t < WIN_END and (t.hour >= 22 or t.hour < 6)]
    for ms in series["temperature_2m"]:
        vals = [ms[i] for i in night_idx if ms[i] is not None]
        if vals:
            night_min_members.append(min(vals))
    night_min_members.sort()
    night_min = {f"p{p}": (round(percentile(night_min_members, p), 1)
                           if night_min_members else None)
                 for p in PERCENTILES}

    # festival headline stats over the whole window
    rain_p50 = sum((b["precipitation"]["p50"] or 0) for b in out_buckets)
    rain_p90 = sum((b["precipitation"]["p90"] or 0) for b in out_buckets)
    temp_high_p50 = max((b["temperature_2m"]["p50"] for b in out_buckets
                         if b["temperature_2m"]["p50"] is not None), default=None)
    gust_max_p90 = max((b["wind_gusts_10m"]["p90"] for b in out_buckets
                        if b["wind_gusts_10m"]["p90"] is not None), default=None)

    # wettest play-hour bucket (afternoon/evening/night) by median rain
    play = [b for b in out_buckets if b["daypart"] in ("odpoledne", "večer", "noc")]
    wettest = max(play, key=lambda b: (b["precipitation"]["p50"] or 0), default=None)

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "model": MODEL, "members": n_members,
        "window": {"start": WIN_START.isoformat(), "end": WIN_END.isoformat()},
        "buckets": out_buckets,
        "night_min": night_min,
        "headline": {
            "rain_total_p50_mm": round(rain_p50, 1),
            "rain_total_p90_mm": round(rain_p90, 1),
            "temp_high_p50_c": temp_high_p50,
            "night_min_p50_c": night_min["p50"],
            "gust_max_p90_kmh": gust_max_p90,
            "wettest_play_bucket": wettest["label"] if wettest else None,
            "wettest_play_rain_p50_mm": (wettest["precipitation"]["p50"]
                                         if wettest else None),
        },
    }


# -------------------------------------------------------------- diffing ----
def diff_vs_last(now, last):
    """Describe what changed vs the previous run (Czech)."""
    if not last:
        return "první běh, není s čím porovnat"
    parts = []
    h0, h1 = last.get("headline", {}), now["headline"]

    d_rain = (h1["rain_total_p50_mm"] or 0) - (h0.get("rain_total_p50_mm") or 0)
    if abs(d_rain) >= 1:
        parts.append(f"medián srážek {'+' if d_rain>=0 else ''}{d_rain:.0f} mm")

    w0, w1 = h0.get("wettest_play_bucket"), h1["wettest_play_bucket"]
    if w0 and w1 and w0 != w1:
        parts.append(f"hlavní déšť se posunul z „{w0}“ na „{w1}“")

    d_hi = ((h1["temp_high_p50_c"] or 0) - (h0.get("temp_high_p50_c") or 0))
    if abs(d_hi) >= 1:
        word = "oteplení" if d_hi > 0 else "ochlazení"
        parts.append(f"{word} přes den o {abs(d_hi):.0f} °C")

    d_nl = ((h1["night_min_p50_c"] or 0) - (h0.get("night_min_p50_c") or 0))
    if abs(d_nl) >= 1:
        word = "teplejší" if d_nl > 0 else "chladnější"
        parts.append(f"noci {word} o {abs(d_nl):.0f} °C")

    d_g = ((h1["gust_max_p90_kmh"] or 0) - (h0.get("gust_max_p90_kmh") or 0))
    if abs(d_g) >= 5:
        parts.append(f"nárazy {'silnější' if d_g>0 else 'slabší'} o {abs(d_g):.0f} km/h")

    return "; ".join(parts) if parts else "beze změny oproti minulému běhu"


def reliability(now):
    """Crude spread-based confidence: narrow p10-p90 rain band => higher."""
    spread = (now["headline"]["rain_total_p90_mm"] or 0) - \
             (now["headline"]["rain_total_p50_mm"] or 0)
    n = now["members"]
    if n < 5:
        return "nízká (málo členů)"
    if spread <= 3:
        return "vysoká (členové se shodují)"
    if spread <= 10:
        return "střední (mírný rozptyl)"
    return "nízká (velký rozptyl členů)"


def email_body(now, change):
    h = now["headline"]
    rel = reliability(now)
    s_heat = (f"Vedro: přes den medián kolem {h['temp_high_p50_c']:.0f} °C, "
              f"noční minimum (komfort) kolem {h['night_min_p50_c']:.0f} °C.")
    if (h["rain_total_p50_mm"] or 0) < 1:
        s_rain = (f"Déšť: v hracích hodinách převážně sucho "
                  f"(medián {h['rain_total_p50_mm']:.0f} mm, "
                  f"smolný scénář p90 {h['rain_total_p90_mm']:.0f} mm).")
    else:
        s_rain = (f"Déšť: v hracích hodinách medián {h['rain_total_p50_mm']:.0f} mm, "
                  f"nejvíc „{h['wettest_play_bucket']}“ "
                  f"({h['wettest_play_rain_p50_mm']:.0f} mm), "
                  f"smolný scénář p90 {h['rain_total_p90_mm']:.0f} mm.")
    s_wind = f"Vítr: nárazy v nepříznivém scénáři (p90) až {h['gust_max_p90_kmh']:.0f} km/h."
    s_change = f"Změna vs. minule: {change}."
    s_rel = f"Spolehlivost předpovědi: {rel}."
    return "\n".join([s_heat, s_rain, s_wind, s_change, s_rel])


# ------------------------------------------------------------ emailing ----
def maybe_send_email(subject, body):
    """Send the summary over SMTP if SMTP_* env vars are set (used by the
    GitHub Action). Returns True if an email was sent, False if not configured.

    Recognised env: SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS,
    MAIL_TO (comma-separated), MAIL_FROM (default SMTP_USER). Port 465 => SSL,
    otherwise STARTTLS."""
    host = os.environ.get("SMTP_HOST")
    mail_to = os.environ.get("MAIL_TO")
    if not host or not mail_to:
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    mail_from = os.environ.get("MAIL_FROM") or user or mail_to
    recipients = [a.strip() for a in mail_to.split(",") if a.strip()]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            if user and pw:
                s.login(user, pw)
            s.sendmail(mail_from, recipients, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            if user and pw:
                s.login(user, pw)
            s.sendmail(mail_from, recipients, msg.as_string())
    return True


# ----------------------------------------------------------------- main ----
def main():
    offline = None
    for a in sys.argv[1:]:
        if a.startswith("--offline="):
            offline = a.split("=", 1)[1]

    data = fetch(offline_path=offline)
    now = analyse(data)

    last = None
    if os.path.exists(LAST_PATH):
        try:
            with open(LAST_PATH) as fh:
                last = json.load(fh)
        except Exception:
            last = None

    change = diff_vs_last(now, last)
    now["change_vs_last"] = change
    body = email_body(now, change)
    now["email_body"] = body

    with open(LAST_PATH, "w") as fh:
        json.dump(now, fh, ensure_ascii=False, indent=2)
    with open(BODY_PATH, "w") as fh:
        fh.write(body + "\n")

    print(body)

    d = datetime.now()
    subject = f"Ostrava BFL počasí — {d.day}.{d.month}.{d.year}"
    try:
        sent = maybe_send_email(subject, body)
        print("\n---\nemail:", "sent via SMTP" if sent
              else "SMTP not configured (set SMTP_HOST/MAIL_TO to send)")
    except Exception as exc:  # don't lose the run if mail fails
        print("\n---\nemail: SEND FAILED:", exc, file=sys.stderr)

    print("full run saved to", LAST_PATH)


if __name__ == "__main__":
    main()
