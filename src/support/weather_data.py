"""Normalize daily weather without replacing missing measurements with zeros."""

import math
from datetime import date, timedelta

from support.constants import WEATHER_VARIABLES


# Keeps real zeros while rejecting missing values and provider fill codes.
def weather_number(value, *, nonnegative=False, fill_value=None):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or value in {-999, -9999} or value == fill_value:
        return None
    return None if nonnegative and value < 0 else value


def requested_dates(parameters):
    start = date.fromisoformat(parameters["start_date"])
    end = date.fromisoformat(parameters["end_date"])
    if end < start:
        raise ValueError("Weather end date precedes start date")
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


# Aligns the response with the requested dates and retains provider and unit metadata.
def normalize_daily(daily, parameters, *, provider, units, time_basis, fill_value=None):
    if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
        return None
    times = daily["time"]
    if any(not isinstance(day, str) for day in times) or len(times) != len(set(times)):
        return None
    fields = list(WEATHER_VARIABLES)
    if "precipitation_sum" in daily:
        fields.append("precipitation_sum")
    if any(key in daily and (not isinstance(daily[key], list) or len(daily[key]) != len(times)) for key in fields):
        return None
    lookup = {day: i for i, day in enumerate(times)}
    dates = requested_dates(parameters)
    output = {"time": dates, "provider": provider, "units": units, "time_basis": time_basis}
    for key in fields:
        values = daily.get(key)
        output[key] = [weather_number(values[lookup[day]], nonnegative=key.endswith("_sum"), fill_value=fill_value)
                       if values is not None and day in lookup else None for day in dates]
    return output if any(value is not None for key in WEATHER_VARIABLES for value in output[key]) else None


# Counts available values per requested variable without treating a present array as complete data.
def valid_day_counts(daily):
    daily = daily or {}
    return {key: sum(weather_number(value, nonnegative=key.endswith("_sum")) is not None
                     for value in (daily.get(key) or [])) for key in WEATHER_VARIABLES}
