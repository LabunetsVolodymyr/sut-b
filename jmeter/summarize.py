"""Підсумок результатів JMeter (.jtl) і перевірка thresholds.

Використання:
    python jmeter/summarize.py results-baseline.jtl baseline
    python jmeter/summarize.py results-load.jtl load
    python jmeter/summarize.py results-stress.jtl stress

Thresholds ті самі, що в k6/scenario.js.
"""
import csv
import math
import sys

# Враховуємо лише 4 запити сценарію (пауза "Think time" в підсумок не входить)
LABELS = {"POST /students", "POST /courses", "POST /enrollments", "GET /enrollments/{id}"}

# профіль: (p95 мс, avg мс або None, error rate %)
LIMITS = {
    "baseline": (500, 300, 1.0),
    "load": (500, 300, 1.0),
    "stress": (1500, None, 5.0),
}


def main(path, profile):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["label"] in LABELS:
                rows.append(r)
    if not rows:
        raise SystemExit("У файлі немає запитів сценарію")

    elapsed = sorted(int(r["elapsed"]) for r in rows)
    n = len(elapsed)
    avg = sum(elapsed) / n
    p95 = elapsed[max(0, math.ceil(0.95 * n) - 1)]
    start = min(int(r["timeStamp"]) for r in rows)
    end = max(int(r["timeStamp"]) + int(r["elapsed"]) for r in rows)
    throughput = n / ((end - start) / 1000)
    errors = sum(1 for r in rows if r["success"] != "true")
    error_rate = errors / n * 100

    print(f"Запитів:        {n}")
    print(f"Average:        {avg:.1f} мс")
    print(f"p95:            {p95} мс")
    print(f"Throughput:     {throughput:.2f} req/s")
    print(f"Error rate:     {error_rate:.2f} %")

    p95_limit, avg_limit, err_limit = LIMITS[profile]
    checks = [(f"p95 < {p95_limit} мс", p95 < p95_limit),
              (f"error rate < {err_limit} %", error_rate < err_limit)]
    if avg_limit is not None:
        checks.append((f"avg < {avg_limit} мс", avg < avg_limit))
    print()
    for name, ok in checks:
        print(("PASS  " if ok else "FAIL  ") + name)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[2] not in LIMITS:
        raise SystemExit("Використання: python summarize.py <results.jtl> baseline|load|stress")
    main(sys.argv[1], sys.argv[2])