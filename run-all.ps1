param(
    [string]$JM = "C:\Users\Vova\apache-jmeter-5.6.3\bin\jmeter.bat"
)

$env:PYTHONIOENCODING = "utf-8"
$env:K6_WEB_DASHBOARD = "true"

try {
    Invoke-WebRequest "http://127.0.0.1:8080/docs" -UseBasicParsing | Out-Null
} catch {
    Write-Host "API is not running on http://127.0.0.1:8080" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $JM)) {
    Write-Host "JMeter not found: $JM" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path "k6\scenario.js") -or -not (Test-Path "jmeter\scenario.jmx")) {
    Write-Host "k6\scenario.js or jmeter\scenario.jmx is missing." -ForegroundColor Red
    exit 1
}

Get-Process k6 -ErrorAction SilentlyContinue | Stop-Process -Force

New-Item -ItemType Directory -Force -Path results | Out-Null

function Clear-Db {
    python -c "import psycopg; c=psycopg.connect('host=localhost dbname=courses_variant12_test user=courses_student password=student123', autocommit=True); c.execute('TRUNCATE enrollments, students, courses RESTART IDENTITY CASCADE'); print('CLEAN')"
}

function Run-K6($type, $name) {
    Write-Host ""
    Write-Host "=== k6 $name ===" -ForegroundColor Cyan
    Clear-Db
    $json = "results\k6-$name.json"
    $txt = "results\k6-$name.txt"
    $env:K6_WEB_DASHBOARD_EXPORT = "results\k6-$name.html"
    k6 run -e TEST_TYPE=$type --summary-export=$json k6/scenario.js | Tee-Object -FilePath $txt
    Start-Sleep -Seconds 5
}

function Run-JMeter($type, $name, $threads, $rampup, $duration, $think) {
    Write-Host ""
    Write-Host "=== JMeter $name ===" -ForegroundColor Cyan
    Clear-Db
    $jtl = "results\jmeter-$name.jtl"
    $report = "results\jmeter-$name-report"
    Remove-Item -Recurse -Force $report -ErrorAction SilentlyContinue
    Remove-Item -Force $jtl -ErrorAction SilentlyContinue
    & $JM -n -t jmeter\scenario.jmx "-Jthreads=$threads" "-Jrampup=$rampup" "-Jduration=$duration" "-Jthink=$think" -l $jtl -e -o $report
    python jmeter\summarize.py $jtl $type
    python jmeter\summarize.py $jtl $type | Out-File -Encoding utf8 "results\jmeter-$name.txt"
    Start-Sleep -Seconds 5
}

Run-K6 "baseline" "baseline"
Run-K6 "load" "load"
Run-K6 "stress" "stress-run1"
Run-K6 "stress" "stress-run2"

Run-JMeter "baseline" "baseline" 1 1 30 500
Run-JMeter "load" "load" 20 20 80 500
Run-JMeter "stress" "stress-run1" 150 80 110 100
Run-JMeter "stress" "stress-run2" 150 80 110 100

Write-Host ""
Write-Host "DONE. See the results folder." -ForegroundColor Green
