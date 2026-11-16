import json
import time
import urllib.request
from pathlib import Path

import psycopg
import pytest
from testcontainers.core.container import DockerContainer

# Шлях до SQL-міграції самого SUT B: <корінь проєкту>/migrations/V1__create_tables.sql
MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "V1__create_tables.sql"


class WireMockClient:
    """Керування WireMock через admin API."""

    def __init__(self, base_url: str):
        # Базова адреса WireMock у контейнері (host і випадковий port)
        self.base_url = base_url

    def _call(self, method, path, payload=None):
        # Універсальний виклик admin API: серіалізує payload у JSON,
        # відправляє реальний HTTP-запит і повертає розібрану JSON-відповідь
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else None

    def reset(self):
        # Очищає і mappings, і request journal
        self._call("POST", "/__admin/reset")

    def stub_policy(self, status=200, body=None):
        # Налаштовує відповідь WireMock на POST /enrollment-policy:
        # який статус і яке тіло повернути (дозволено, відмова, 503 тощо)
        response = {"status": status, "headers": {"Content-Type": "application/json"}}
        if body is not None:
            # Тіло може бути словником або готовим рядком
            response["body"] = body if isinstance(body, str) else json.dumps(body)
        # Реєструємо mapping: "коли приходить такий запит, віддати таку відповідь"
        self._call("POST", "/__admin/mappings", {
            "request": {"method": "POST", "url": "/enrollment-policy"},
            "response": response,
        })

    def policy_requests(self):
        # Повертає з request journal усі запити SUT до /enrollment-policy.
        # За ними перевіряємо кількість, метод, endpoint і тіло запиту
        result = self._call("POST", "/__admin/requests/find",
                            {"method": "POST", "url": "/enrollment-policy"})
        return result["requests"]


@pytest.fixture(scope="session")
def db_params():
    """Запускає PostgreSQL у контейнері й застосовує migration."""
    # scope="session": контейнер стартує один раз перед першим тестом
    # і зупиняється після останнього
    container = (DockerContainer("postgres:17")
                 # Credentials задаємо змінними середовища контейнера
                 .with_env("POSTGRES_USER", "lab5")
                 .with_env("POSTGRES_PASSWORD", "lab5pass")
                 .with_env("POSTGRES_DB", "courses_lab5")
                 .with_exposed_ports(5432))  # порт на хості обирається випадково
    container.start()
    try:
        # Параметри підключення отримуємо з контейнера під час роботи,
        # у коді немає жодного hardcoded порту хоста
        # URL і PORT: беремо з контейнера під час роботи, а не з файлів чи змінних
        params = dict(
            host=container.get_container_host_ip(),  # адреса, зазвичай localhost
            port=int(container.get_exposed_port(5432)),  # випадковий порт на хості
            dbname="courses_lab5", user="lab5", password="lab5pass")  # ті самі credentials


        # Чекаємо, поки PostgreSQL почне приймати з'єднання
        # (до 60 спроб із паузою в 1 секунду)
        for _ in range(60):
            try:
                with psycopg.connect(**params, connect_timeout=2) as c:
                    c.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                time.sleep(1)
        else:
            raise RuntimeError("PostgreSQL у контейнері не запустився")

        # Автоматично застосовуємо migration з SUT B до порожньої БД
        # (один раз на сесію, до першого тесту; ручних дій немає)
        with psycopg.connect(**params, autocommit=True) as c:
            c.execute(MIGRATION.read_text(encoding="utf-8"))

        yield params  # віддаємо параметри тестам
    finally:
        container.stop()  # зупиняємо контейнер навіть якщо тести впали


@pytest.fixture(scope="session")
def wiremock_url():
    """Запускає WireMock у контейнері."""
    # Теж один раз на сесію; 8080 — внутрішній порт WireMock у контейнері
    container = (DockerContainer("wiremock/wiremock:3.9.1")
                 .with_exposed_ports(8080))
    container.start()
    try:
        # Адреса збирається з host і випадкового порту, який видав Docker
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        # Чекаємо, поки WireMock почне відповідати на admin API
        for _ in range(60):
            try:
                urllib.request.urlopen(url + "/__admin/mappings", timeout=2)
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("WireMock у контейнері не запустився")
        yield url  # цю адресу отримує HttpEnrollmentPolicy
    finally:
        container.stop()


@pytest.fixture
def conn(db_params):
    """Нове з'єднання; перед кожним тестом БД очищається."""
    # Без scope: виконується перед кожним тестом, це забезпечує незалежність.
    # TRUNCATE видаляє всі дані (але не структуру), RESTART IDENTITY скидає лічильники id
    with psycopg.connect(**db_params, autocommit=True) as c:
        c.execute("TRUNCATE enrollments, students, courses RESTART IDENTITY CASCADE")
        yield c


@pytest.fixture
def wiremock(wiremock_url):
    """Перед кожним тестом очищає mappings і request journal."""
    # Без reset запити й налаштування попереднього тесту вплинули б на наступний
    client = WireMockClient(wiremock_url)
    client.reset()
    return client