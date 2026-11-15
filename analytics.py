"""Explainable estimates based on imported transaction history."""
from collections import Counter, defaultdict
from datetime import datetime
import math


def register_forecast(rows, tz, service_minutes, *, include_profile=False):
    days = defaultdict(dict)
    for row in rows:
        stamp = datetime.fromisoformat(row["occurred_at"]).astimezone(tz)
        counts = days[stamp.weekday()].setdefault(stamp.date(), Counter())
        counts[stamp.hour] += 1

    profiles, total_error, predictions = [], 0, 0
    for weekday in sorted(days):
        history = days[weekday]
        cumulative = Counter()
        for index, day in enumerate(sorted(history)):
            counts = history[day]
            if index >= 3:
                total_error += sum(abs(cumulative[hour] / index - counts[hour]) for hour in range(24))
                predictions += 24
            cumulative.update(counts)

        observed = len(history)
        if observed < 3:
            continue
        for hour in range(24):
            rate = cumulative[hour] / observed
            required = math.ceil(rate * service_minutes / 48)
            profiles.append((weekday, hour, observed, round(rate, 1), min(2, max(1, required)), required > 2))
    peak_hours = sorted(
        (profile for profile in profiles if profile[3] > 0),
        key=lambda profile: (-profile[3], profile[0], profile[1]),
    )[:10]
    mae = round(total_error / predictions, 2) if predictions else None
    if include_profile:
        indexed = {(row[0], row[1]): row for row in profiles}
        schedule = [[indexed.get((weekday, hour)) for hour in range(24)] for weekday in range(7)]
        return peak_hours, mae, predictions, schedule
    return peak_hours, mae, predictions


def pos_exception_signal(rows, tz):
    days = defaultdict(lambda: [0, 0])
    for row in rows:
        day = datetime.fromisoformat(row["occurred_at"]).astimezone(tz).date()
        days[day][0] += 1
        days[day][1] += row["exception"]
    if len(days) < 8:
        return None
    latest = max(days)
    count, exceptions = days[latest]
    eligible = [day for day in sorted(days) if 0 < (latest - day).days <= 28]
    baseline_total = sum(days[day][0] for day in eligible)
    if count < 20 or exceptions < 5 or baseline_total < 100 or len(eligible) < 7:
        return None
    baseline = sum(days[day][1] for day in eligible) / baseline_total
    current = exceptions / count
    if current >= max(baseline * 2, baseline + 0.05):
        return latest, exceptions, count, round(current * 100, 1), round(baseline * 100, 1)
    return None
