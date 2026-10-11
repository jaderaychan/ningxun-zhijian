"""Rainfall forecasts, as an input the analyst can check the user against.

Why this exists
---------------
Until now the agent only ever saw the rainfall number the user typed. If someone
said "100 mm tomorrow", nothing could tell them that no model forecasts any rain
at all. With this module the agent can look, and then say so.

Design decisions worth keeping
------------------------------
* **A forecast is an interval, not a number.** Open-Meteo is asked for several
  independent models in one call, and the reported spread is the spread between
  those models. That is a real, data-derived range. A single-model source is
  reported as such instead of being dressed up with an invented +/- 50%.
* **Fail closed.** A network error, a quota error or an unparseable payload
  returns ok=False. It never degrades to zero rainfall, because "no rain" and
  "could not ask" must not look the same to the caller.
* **Always say when it was fetched.** Forecasts are perishable; every result
  carries `retrieved_at` and the model run it came from.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
QWEATHER_PATH = "/v7/weather/24h"

# Independent models, deliberately from different forecasting centres so their
# disagreement means something. Asking one centre for three resolutions would
# produce a spread that is mostly cosmetic.
DEFAULT_MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless")

MODEL_LABELS = {
    "ecmwf_ifs025": "ECMWF IFS 0.25°（欧洲中期天气预报中心）",
    "gfs_seamless": "NOAA GFS（美国国家气象局）",
    "icon_seamless": "DWD ICON（德国气象局）",
}

DEFAULT_TZ = "Asia/Shanghai"
CACHE_TTL_SECONDS = 1800  # forecasts are reissued every few hours; 30 min is plenty

# Project scope: the coming 15 days, and nothing outside it.
#
# 15 days sits just inside what Open-Meteo will serve (16), and that is
# deliberate: the project limit stays the binding constraint, so there is only
# ever one reason to refuse. A scope wider than the horizon would force the tool
# to distinguish "we do not do that" from "we cannot see that yet", and the
# second message is only useful when the first is not blurring it.
SCOPE_DAYS_AHEAD = 15
SCOPE_PAST_TOLERANCE_HOURS = 24

# Open-Meteo will not serve more than this, so it is requested in full. Asking
# for less would make the tool refuse requests the models could have answered,
# and would blame the models for a limit we imposed ourselves.
MAX_FORECAST_DAYS = 16


def _cache_dir(root: str) -> str:
    path = os.path.join(root, "data", "weather")
    os.makedirs(path, exist_ok=True)
    return path


def check_time_scope(moment: datetime, now: datetime) -> dict | None:
    """Return an error payload when `moment` is outside the project's window.

    None means the time is acceptable. Callers must treat a refusal as final:
    there is no forecast to fall back on, because the question itself is out of
    scope rather than merely unavailable.
    """
    latest = now + timedelta(days=SCOPE_DAYS_AHEAD)
    earliest = now - timedelta(hours=SCOPE_PAST_TOLERANCE_HOURS)
    window = {"earliest": earliest.strftime("%Y-%m-%dT%H:%M"),
              "latest": latest.strftime("%Y-%m-%dT%H:%M")}
    if moment > latest:
        return {
            "ok": False,
            "code": "TIME_OUT_OF_SCOPE",
            "message": "本项目只处理未来 %d 天之内（%s 至 %s）的情景，%s 超出了这个范围。"
                       % (SCOPE_DAYS_AHEAD, earliest.strftime("%Y-%m-%d"),
                          latest.strftime("%Y-%m-%d"), moment.strftime("%Y-%m-%d")),
            "scope": window,
        }
    if moment < earliest:
        return {
            "ok": False,
            "code": "TIME_OUT_OF_SCOPE",
            "message": "本项目只处理未来 %d 天之内（%s 至 %s）的情景，%s 已经是过去。"
                       % (SCOPE_DAYS_AHEAD, earliest.strftime("%Y-%m-%d"),
                          latest.strftime("%Y-%m-%d"), moment.strftime("%Y-%m-%d")),
            "scope": window,
        }
    return None


def _http_json(url: str, timeout: int) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "floodkit/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _cached(url: str, cache_file: str, timeout: int) -> tuple[dict, bool]:
    """Return (payload, from_cache). A stale cache is only used when the network fails."""
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < CACHE_TTL_SECONDS:
            try:
                return json.load(open(cache_file, encoding="utf-8")), True
            except Exception:
                pass
    payload = _http_json(url, timeout)
    try:
        json.dump(payload, open(cache_file, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return payload, False


def _hourly_times(payload: dict) -> list[str]:
    return list((payload.get("hourly") or {}).get("time") or [])


def _key(name: str) -> str:
    return "precipitation_" + name


def fetch_hourly(
    lat: float,
    lon: float,
    root: str,
    models: tuple[str, ...] = DEFAULT_MODELS,
    forecast_days: int = MAX_FORECAST_DAYS,
    past_days: int = 1,
    tz: str = DEFAULT_TZ,
    timeout: int = 25,
) -> dict:
    """Raw hourly precipitation for each model. ok=False on any failure."""
    url = "%s?%s" % (FORECAST_URL, urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "hourly": "precipitation",
        "models": ",".join(models),
        "forecast_days": forecast_days,
        "past_days": past_days,
        "timezone": tz,
    }))
    tag = "openmeteo_%.3f_%.3f_%dd_%dd_%s" % (lat, lon, forecast_days, past_days, tz.replace("/", "-"))
    cache_file = os.path.join(_cache_dir(root), tag + ".json")
    try:
        payload, from_cache = _cached(url, cache_file, timeout)
    except Exception as error:
        if os.path.exists(cache_file):
            try:
                payload = json.load(open(cache_file, encoding="utf-8"))
                return {
                    "ok": True,
                    "source": "Open-Meteo",
                    "retrieved_at": datetime.fromtimestamp(
                        os.path.getmtime(cache_file)).isoformat(timespec="seconds"),
                    "stale_cache": True,
                    "warning": "网络请求失败（%s），使用了本次会话之前的缓存预报。" % error,
                    "payload": payload,
                }
            except Exception:
                pass
        return {
            "ok": False,
            "code": "FORECAST_UNAVAILABLE",
            "source": "Open-Meteo",
            "message": "拿不到预报：%s。不要假设没有降雨。" % error,
        }
    return {
        "ok": True,
        "source": "Open-Meteo",
        "retrieved_at": datetime.now().isoformat(timespec="seconds"),
        "from_cache": from_cache,
        "payload": payload,
    }


def _qweather(lat: float, lon: float, timeout: int) -> dict | None:
    """Optional second opinion from QWeather, when the account is configured.

    QWeather's free hourly product only reaches ~24 h ahead, so it is used as a
    cross-check when it covers the requested hour and skipped when it does not.
    """
    key = os.environ.get("QWEATHER_KEY")
    host = os.environ.get("QWEATHER_HOST")
    if not key or not host:
        return None
    url = "https://%s%s?%s" % (host.strip("/"), QWEATHER_PATH,
                               urllib.parse.urlencode({"location": "%.4f,%.4f" % (lon, lat), "key": key}))
    try:
        payload = _http_json(url, timeout)
    except Exception as error:
        return {"ok": False, "source": "QWeather", "message": str(error)}
    if str(payload.get("code")) != "200":
        return {"ok": False, "source": "QWeather", "message": "接口返回 code=%s" % payload.get("code")}
    hourly = [
        {"time": h.get("fxTime"), "precip_mm": float(h.get("precip") or 0.0)}
        for h in (payload.get("hourly") or [])
    ]
    return {"ok": True, "source": "QWeather（和风天气）", "hourly": hourly}


def window_forecast(
    lat: float,
    lon: float,
    at_local: str,
    root: str,
    window_hours: int = 1,
    models: tuple[str, ...] = DEFAULT_MODELS,
    tz: str = DEFAULT_TZ,
    forecast_days: int = MAX_FORECAST_DAYS,
    past_days: int = 1,
    timeout: int = 25,
    now: datetime | None = None,
) -> dict:
    """Forecast rainfall over a window, with the spread between models.

    `at_local` is a naive local time such as "2026-10-11T12:00"; the window runs
    from that hour for `window_hours` hours. `past_days` is included so a time
    that has just passed can still be looked up for review.
    """
    try:
        start = datetime.fromisoformat(at_local)
    except Exception:
        return {"ok": False, "code": "BAD_TIME",
                "message": "时间格式无法解析：%r（应为 2026-10-11T12:00 这样的本地时间）" % at_local}

    # The series is hourly, so a start carrying minutes can never match a stamp:
    # "2026-10-26T10:14" is not a key when the keys are 10:00, 11:00, … Every model
    # then yields None and the request comes back as OUT_OF_HORIZON, whose message
    # compares 10:14 against a horizon of 23:00 and contradicts itself. A time that
    # is in range and fully served was being reported as beyond the data.
    #
    # The docstring already fixes the semantic — the window runs "from that hour" —
    # so snap down to the containing hour and say so, rather than silently serving a
    # different hour than the one asked for.
    snapped_from = None
    floored = start.replace(minute=0, second=0, microsecond=0)
    if floored != start:
        snapped_from = start.strftime("%Y-%m-%dT%H:%M")
        start = floored

    now_local = now or datetime.now()
    refusal = check_time_scope(start, now_local)
    if refusal is not None:
        return refusal

    raw = fetch_hourly(lat, lon, root, models=models, forecast_days=forecast_days,
                       past_days=past_days, tz=tz, timeout=timeout)
    if not raw.get("ok"):
        return raw

    payload = raw["payload"]
    times = _hourly_times(payload)
    if not times:
        return {"ok": False, "code": "EMPTY_FORECAST", "message": "预报返回了空的小时序列。"}

    end = start + timedelta(hours=max(1, int(window_hours)))
    wanted = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
              for i in range(max(1, int(window_hours)))]

    per_model: dict[str, float | None] = {}
    hourly_rows = []
    for key_name, label in MODEL_LABELS.items():
        series = (payload.get("hourly") or {}).get(_key(key_name))
        if series is None:
            continue
        picked = []
        for stamp in wanted:
            if stamp in times:
                value = series[times.index(stamp)]
                picked.append(0.0 if value is None else float(value))
        per_model[key_name] = round(sum(picked), 2) if picked else None

    covered = [v for v in per_model.values() if v is not None]
    if not covered:
        horizon = "%s .. %s" % (times[0], times[-1]) if times else "未知"
        # Distinct from TIME_OUT_OF_SCOPE on purpose: this time IS in scope, the
        # models simply cannot see that far ahead yet. Telling the user "out of
        # scope" here would be wrong, and would hide a working request behind a
        # refusal.
        return {
            "ok": False,
            "code": "OUT_OF_HORIZON",
            "source": raw["source"],
            "message": "时间在项目范围内（未来 %d 天内），但预报数据只到 %s，看不到 %s。"
                       "这是数据时限，不是范围问题；临近了再查。"
                       % (SCOPE_DAYS_AHEAD, times[-1] if times else "未知", wanted[0]),
            "available_range": [times[0], times[-1]] if times else None,
        }

    in_past = start < now_local - timedelta(hours=1)

    # A little context around the window, so the answer can say "the whole day is
    # dry" rather than only "that one hour is dry".
    context = []
    for stamp in times:
        if stamp[:10] == start.strftime("%Y-%m-%d"):
            row = {"time": stamp}
            for key_name in MODEL_LABELS:
                series = (payload.get("hourly") or {}).get(_key(key_name))
                if series is not None and stamp in times:
                    value = series[times.index(stamp)]
                    row[key_name] = 0.0 if value is None else round(float(value), 2)
            context.append(row)

    qw = _qweather(lat, lon, timeout)

    ordered = sorted(covered)
    median = ordered[len(ordered) // 2] if len(ordered) % 2 else \
        round((ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2.0, 2)

    result = {
        "ok": True,
        "source": raw["source"] + "（%d 个独立模式）" % len(covered),
        "retrieved_at": raw["retrieved_at"],
        "from_cache": raw.get("from_cache", False),
        "point": {"lat": lat, "lon": lon},
        "timezone": tz,
        "window": {
            "start": start.strftime("%Y-%m-%dT%H:%M"),
            "end": end.strftime("%Y-%m-%dT%H:%M"),
            "hours": max(1, int(window_hours)),
            "is_in_past": in_past,
            # Present only when the requested time carried minutes and was served by
            # the containing hour. The caller can then say which hour it actually got.
            "snapped_from": snapped_from,
        },
        "per_model_mm": per_model,
        "min_mm": ordered[0],
        "max_mm": ordered[-1],
        "median_mm": median,
        "spread_source": "预报模式之间的差异（%d 个来自不同气象中心的模式）" % len(covered)
                         if len(covered) > 1 else "只有一个模式，无法给出模式间差异",
        "day_context": context,
    }
    if raw.get("warning"):
        result["warning"] = raw["warning"]
    if qw is not None:
        result["qweather"] = qw
    if in_past:
        result["warning_past"] = "请求的时间已经过去，这不是预报而是回顾；请确认用户是否想复盘。"
    return result


def summarise(result: dict) -> str:
    """One English line for the host summary. Chinese detail lives in the payload."""
    if not result.get("ok"):
        return "weather: %s" % result.get("code", "unavailable")
    return "weather: %.1f-%.1f mm forecast over %s" % (
        result["min_mm"], result["max_mm"], result["window"]["start"])
