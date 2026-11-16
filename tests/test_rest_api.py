import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent  # корінь sut-b (звідси запускається uvicorn)
TIMEOUT = 5  # секунд на кожен HTTP-запит


def uid() -> str:
    # Унікальний суфікс для code/reference, щоб ресурси не конфліктували
    # (у БД ці поля унікальні)
    return uuid.uuid4().hex[:10]


# ------------------------- запуск SUT (REST API) ----------------------------

@pytest.fixture(scope="session")
def api_url(db_params, tmp_path_factory):
    """Запускає справжній uvicorn на вільному порту з БД із контейнера."""
    # scope="session": сервер стартує один раз перед першим тестом
    # і зупиняється після останнього.
    # db_params (з conftest.py) підіймає PostgreSQL у контейнері й застосовує migration

    # Вільний порт обираємо динамічно: bind на порт 0 віддає вільний порт від ОС,
    # тому hardcoded порту немає
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    # Параметри БД беремо з контейнера. API читає їх зі змінних середовища
    # (DB_HOST, DB_PORT, ...), тому передаємо їх процесу сервера
    env = os.environ.copy()
    env.update(
        DB_HOST=str(db_params["host"]), DB_PORT=str(db_params["port"]),
        DB_NAME=db_params["dbname"], DB_USER=db_params["user"],
        DB_PASSWORD=db_params["password"])
    # Без ENROLLMENT_POLICY_URL API використовує локальну політику, що дозволяє все,
    # тож перевіряються правила сервісу, а не зовнішній сервіс
    env.pop("ENROLLMENT_POLICY_URL", None)

    # Лог сервера пишемо у файл: якщо старт не вдасться, його видно в помилці
    log_path = tmp_path_factory.mktemp("api") / "uvicorn.log"
    with open(log_path, "w") as log:
        # Справжній сервер як окремий процес; sys.executable це python із .venv
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "courses.api:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        url = f"http://127.0.0.1:{port}"
        try:
            # Чекаємо, поки API почне відповідати (до 60 спроб)
            for _ in range(60):
                if proc.poll() is not None:  # процес уже завершився: помилка старту
                    raise RuntimeError("uvicorn завершився:\n" + log_path.read_text())
                try:
                    if requests.get(url + "/openapi.json", timeout=2).status_code == 200:
                        break
                except requests.ConnectionError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("API не запустився:\n" + log_path.read_text())
            yield url  # тести отримують базову адресу API
        finally:
            # Зупиняємо сервер навіть якщо тести впали
            proc.terminate()
            proc.wait(timeout=10)


@pytest.fixture(autouse=True)
def clean_db(conn):
    """Перед кожним тестом БД очищається (фікстура conn з conftest.py)."""
    # autouse=True: застосовується до кожного тесту автоматично.
    # Самого очищення тут немає: воно виконується у фікстурі conn (TRUNCATE),
    # яку ми лише підключаємо. Це забезпечує незалежність тестів
    yield


# ----------------------------- допоміжні виклики ----------------------------

def post(api, path, body=None):
    # POST із JSON-тілом (Content-Type виставляє requests сам)
    return requests.post(api + path, json=body, timeout=TIMEOUT)


def get(api, path):
    return requests.get(api + path, timeout=TIMEOUT)


def create_student(api, age=25, score=70):
    # Створює слухача через REST API; перевіряє 201 і повертає тіло відповіді
    # (з id, який видала БД). Унікальний code через uid()
    r = post(api, "/students", {"code": f"S-{uid()}", "age": age,
                                "entry_score": score, "regular": False})
    assert r.status_code == 201, r.text  # r.text покаже причину, якщо не 201
    return r.json()


def create_course(api, price=100_000, capacity=10, level=1):
    # Створює курс через REST API
    r = post(api, "/courses", {"code": f"C-{uid()}", "price": price,
                               "capacity": capacity, "level": level})
    assert r.status_code == 201, r.text
    return r.json()


def enroll_request(student, course):
    # Тіло запиту на зарахування. id слухача й курсу беруться з попередніх
    # відповідей API (динамічно), а не прописані вручну
    return {"reference": f"E-{uid()}", "student_id": student["id"],
            "course_id": course["id"], "coupon_valid": False}


def make_enrollment(api, price=100_000):
    """Створює слухача, курс і заявку; повертає (заявка, слухач, курс)."""
    s, c = create_student(api), create_course(api, price=price)
    r = post(api, "/enrollments", enroll_request(s, c))
    assert r.status_code == 201, r.text
    return r.json(), s, c


def assert_error_contract(response, code):
    """Помилка має JSON {"code": "...", "message": "..."}."""
    body = response.json()
    assert body["code"] == code  # очікуваний код помилки (NOT_FOUND, ENROLLMENT_CONFLICT...)
    assert isinstance(body["message"], str) and body["message"]  # непорожнє повідомлення


# ------------------------------- ТЕСТИ --------------------------------------

def test_post_enrollment_201_location_and_fields(api_url):
    # Підготовка: слухач і курс із ціною 123450
    s, c = create_student(api_url), create_course(api_url, price=123_450)

    r = post(api_url, "/enrollments", enroll_request(s, c))

    assert r.status_code == 201  # ресурс створено
    body = r.json()
    assert isinstance(body["id"], int)  # id отримано з відповіді
    assert r.headers["Location"] == f"/enrollments/{body['id']}"  # посилання на ресурс
    assert body["status"] == "Enrolled"
    # Знижки немає: total = ціна, prepaid = total, refund = 0
    assert (body["total"], body["prepaid"], body["refund"]) == (123_450, 123_450, 0)
    assert (body["student_id"], body["course_id"]) == (s["id"], c["id"])


def test_get_enrollment_200_matches_created(api_url):
    created, _, _ = make_enrollment(api_url)

    r = get(api_url, f"/enrollments/{created['id']}")

    assert r.status_code == 200
    assert r.json() == created  # дані з БД збігаються зі створеним


def test_get_unknown_enrollment_404_error_contract(api_url):
    # Ресурсу з таким id немає (БД очищена перед тестом)
    r = get(api_url, "/enrollments/999999")

    assert r.status_code == 404
    assert_error_contract(r, "NOT_FOUND")  # перевіряємо поля code і message


def test_complete_200_new_state(api_url):
    created, _, _ = make_enrollment(api_url)

    # Action endpoint: завершення курсу (тіло запиту не потрібне)
    r = post(api_url, f"/enrollments/{created['id']}/complete")

    assert r.status_code == 200
    assert r.json()["status"] == "Completed"
    # Окремий GET підтверджує, що новий стан реально збережено
    assert get(api_url, f"/enrollments/{created['id']}").json()["status"] == "Completed"


def test_cancel_200_refund_equals_prepaid(api_url):
    created, _, _ = make_enrollment(api_url, price=20_000)

    # Скасування за 7 днів до початку
    r = post(api_url, f"/enrollments/{created['id']}/cancel", {"days_before_start": 7})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "Cancelled"
    assert body["refund"] == 20_000  # правило варіанта: повертається вся передоплата
    assert (body["total"], body["prepaid"]) == (20_000, 20_000)  # суми незмінні


def test_cancel_completed_409_and_resource_unchanged(api_url):
    created, _, _ = make_enrollment(api_url)
    post(api_url, f"/enrollments/{created['id']}/complete")  # заявка стає Completed
    # Стан ДО відхиленої операції
    before = get(api_url, f"/enrollments/{created['id']}").json()

    # Скасувати завершену заявку заборонено
    r = post(api_url, f"/enrollments/{created['id']}/cancel", {"days_before_start": 7})# заборонена дія

    assert r.status_code == 409
    assert_error_contract(r, "ENROLLMENT_CONFLICT")
    # Стан ПІСЛЯ відхилення: ресурс не змінився
    after = get(api_url, f"/enrollments/{created['id']}").json()
    assert after == before
    assert after["status"] == "Completed" and after["refund"] == 0



def test_repeat_cancel_409_and_resource_unchanged(api_url):
    created, _, _ = make_enrollment(api_url)
    post(api_url, f"/enrollments/{created['id']}/cancel", {"days_before_start": 3})
    before = get(api_url, f"/enrollments/{created['id']}").json()

    # Повторне скасування вже скасованої заявки заборонено
    r = post(api_url, f"/enrollments/{created['id']}/cancel", {"days_before_start": 3})

    assert r.status_code == 409
    assert_error_contract(r, "ENROLLMENT_CONFLICT")
    assert get(api_url, f"/enrollments/{created['id']}").json() == before


def test_invalid_student_400_error_contract(api_url):
    # age не є числом: валідація тіла запиту відхиляє його -> 400 INVALID_REQUEST
    r = post(api_url, "/students", {"code": f"S-{uid()}", "age": "abc",
                                    "entry_score": 70, "regular": False})

    assert r.status_code == 400
    assert_error_contract(r, "INVALID_REQUEST")


# Персональне правило: допуск за рівнем курсу. Та сама послідовність запитів,
# різні дані, тому параметризація.
# Рівень 1: усі; рівень 2: бал від 60; рівень 3: вік від 18 і бал від 80.
@pytest.mark.parametrize("level, age, score, expected", [
    (1, 25, 10, 201),   # рівень 1: усі
    (2, 25, 60, 201),   # рівень 2: межа 60 включна
    (2, 25, 59, 409),   # рівень 2: 59 < 60
    (3, 18, 80, 201),   # рівень 3: обидві межі включні
    (3, 17, 95, 409),   # рівень 3: вік 17 < 18
    (3, 30, 79, 409),   # рівень 3: бал 79 < 80
])
def test_personal_rule_admission_by_course_level(api_url, level, age, score, expected):
    s = create_student(api_url, age=age, score=score)
    c = create_course(api_url, level=level)

    r = post(api_url, "/enrollments", enroll_request(s, c))

    assert r.status_code == expected  # 201 допущено, 409 відмова
    if expected == 409:
        assert_error_contract(r, "ENROLLMENT_CONFLICT")


def test_workflow_post_get_cancel_get_dynamic_ids(api_url):
    # Workflow: POST -> GET -> action -> GET. Усі id беремо з попередніх відповідей
    student = create_student(api_url)
    course = create_course(api_url, price=50_000)
    enrollment = post(api_url, "/enrollments", enroll_request(student, course)).json()
    path = f"/enrollments/{enrollment['id']}"  # шлях збираємо з id відповіді POST

    # GET: заявка існує у стані Enrolled
    first = get(api_url, path)
    assert first.status_code == 200 and first.json()["status"] == "Enrolled"

    # Action: скасування
    cancel = post(api_url, path + "/cancel", {"days_before_start": 10})
    assert cancel.status_code == 200

    # Фінальний GET: стан змінився, повернення дорівнює передоплаті
    last = get(api_url, path).json()
    assert last["status"] == "Cancelled" and last["refund"] == 50_000