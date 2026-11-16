import json

import pytest

from courses.http_enrollment_policy import HttpEnrollmentPolicy, EnrollmentPolicyError
from courses.models import Student, Course, EnrollmentRequest, EnrollmentStatus as S
from courses.repositories import (
    PostgresStudentRepository,
    PostgresCourseRepository,
    PostgresEnrollmentRepository,
)
from courses.service import EnrollmentService, EnrollmentRejectedError


class Ctx:
    """Тестове оточення Lab 5.

    Реальні: EnrollmentService, Postgres-репозиторії (БД у контейнері)
    і HttpEnrollmentPolicy (реальні HTTP-запити до WireMock у контейнері).
    Жодних Mock/Stub на рівні Python.
    """

    def __init__(self, conn, wiremock, wiremock_url):
        self.students = PostgresStudentRepository(conn)
        self.courses = PostgresCourseRepository(conn)
        self.enrollments = PostgresEnrollmentRepository(conn)
        self.wiremock = wiremock
        # Реальна HTTP-політика замість Mock
        self.policy = HttpEnrollmentPolicy(wiremock_url, timeout=2)
        self.service = EnrollmentService(
            self.students, self.courses, self.enrollments, self.policy)

    def student(self, age=25, score=70):
        # Створює слухача в PostgreSQL (БД очищається перед кожним тестом,
        # тому фіксований code не конфліктує між тестами)
        return self.students.create(Student(0, "s-lab5", age, score))

    def course(self, price=10_000, capacity=5, level=1):
        # Створює курс у PostgreSQL
        return self.courses.create(Course(0, "c-lab5", price, capacity, level))

    def request(self, s, c):
        # Запит на зарахування слухача s на курс c
        return EnrollmentRequest("r-lab5", s.id, c.id)


@pytest.fixture
def ctx(conn, wiremock, wiremock_url):
    # conn і wiremock з conftest.py: вони вже очистили БД і WireMock
    # перед цим тестом, тож кожен тест стартує з чистого стану
    return Ctx(conn, wiremock, wiremock_url)


def assert_one_policy_request(ctx, s, c):
    """Перевіряє кількість, метод, endpoint і тіло HTTP-запиту."""
    # Беремо з request journal WireMock усі запити до /enrollment-policy
    requests = ctx.wiremock.policy_requests()
    assert len(requests) == 1  # рівно один запит
    assert requests[0]["method"] == "POST"  # метод
    assert requests[0]["url"] == "/enrollment-policy"  # endpoint
    # Тіло запиту: ідентифікатори саме цього слухача і курсу
    assert json.loads(requests[0]["body"]) == {"studentId": s.id, "courseId": c.id}


def assert_nothing_saved(ctx, c):
    # Негативні сценарії: у БД немає жодної заявки на курс у жодному статусі
    for status in S:
        assert ctx.enrollments.find_enrollments(c.id, status) == []
    assert ctx.enrollments.count_occupied(c.id) == 0


def test_positive_external_allowed_persisted_in_postgres(ctx):
    # Рівень 2, бал 60 (межа персонального правила)
    s, c = ctx.student(score=60), ctx.course(price=12_345, level=2)
    # WireMock відповідає: зарахування дозволено
    ctx.wiremock.stub_policy(200, {"allowed": True})

    result = ctx.service.enroll(ctx.request(s, c))

    stored = ctx.enrollments.find_by_id(result.id)  # реальний стан у PostgreSQL
    assert stored.status == S.ENROLLED
    # total = ціна (знижки немає), prepaid = total, refund = 0
    assert (stored.total, stored.prepaid, stored.refund) == (12_345, 12_345, 0)
    assert (stored.student_id, stored.course_id) == (s.id, c.id)
    # Зовнішній HTTP-сервіс був викликаний з правильним запитом
    assert_one_policy_request(ctx, s, c)


def test_external_rejection_allowed_false_http_called_nothing_saved(ctx):
    s, c = ctx.student(), ctx.course()
    # WireMock коректно відповідає, що зарахування не дозволено
    ctx.wiremock.stub_policy(200, {"allowed": False})

    # SUT перетворює відмову політики на EnrollmentRejectedError
    with pytest.raises(EnrollmentRejectedError):
        ctx.service.enroll(ctx.request(s, c))

    assert_nothing_saved(ctx, c)  # у PostgreSQL заявки немає
    assert_one_policy_request(ctx, s, c)  # зовнішня відмова: HTTP-запит = 1


def test_external_unavailable_503_error_nothing_saved(ctx):
    s, c = ctx.student(), ctx.course()
    # WireMock імітує недоступність сервісу: статус 503 без тіла
    ctx.wiremock.stub_policy(503)

    # SUT не вважає операцію успішною: виникає EnrollmentPolicyError
    with pytest.raises(EnrollmentPolicyError):
        ctx.service.enroll(ctx.request(s, c))

    assert_nothing_saved(ctx, c)  # БД лишилась у коректному стані
    assert_one_policy_request(ctx, s, c)  # запит до сервісу був виконаний


def test_invalid_policy_response_missing_allowed_nothing_saved(ctx):
    s, c = ctx.student(), ctx.course()
    # Статус 200, але в тілі немає обов'язкового логічного поля allowed
    ctx.wiremock.stub_policy(200, {"unexpected": True})

    # Некоректну відповідь SUT теж не сприймає як дозвіл
    with pytest.raises(EnrollmentPolicyError):
        ctx.service.enroll(ctx.request(s, c))

    assert_nothing_saved(ctx, c)
    assert_one_policy_request(ctx, s, c)


def test_local_rejection_personal_rule_no_http_request(ctx):
    # Рівень 3 вимагає вік від 18: слухач 17 років відхиляється до HTTP
    s, c = ctx.student(age=17, score=95), ctx.course(level=3)
    ctx.wiremock.stub_policy(200, {"allowed": True})  # навіть якщо дозволив би

    # Персональне правило спрацьовує раніше за звернення до політики
    with pytest.raises(EnrollmentRejectedError):
        ctx.service.enroll(ctx.request(s, c))

    assert_nothing_saved(ctx, c)
    assert ctx.wiremock.policy_requests() == []  # локальна відмова: HTTP-запит = 0


def test_local_rejection_no_free_seat_no_http_request(ctx):
    # Другий слухач (інший code, бо code унікальний)
    s1, s2 = ctx.student(), ctx.students.create(Student(0, "s2-lab5", 25, 70))
    c = ctx.course(capacity=1)  # лише одне місце
    ctx.wiremock.stub_policy(200, {"allowed": True})
    ctx.service.enroll(ctx.request(s1, c))  # займає єдине місце
    ctx.wiremock.reset()                     # очищаємо journal перед перевіркою

    # Місць немає: відмова ще до звернення до зовнішнього сервісу
    with pytest.raises(EnrollmentRejectedError):
        ctx.service.enroll(EnrollmentRequest("r2-lab5", s2.id, c.id))

    assert ctx.enrollments.count_occupied(c.id) == 1  # лишилась лише перша заявка
    assert ctx.enrollments.has_enrollment(s2.id, c.id) is False
    assert ctx.wiremock.policy_requests() == []  # локальна відмова: HTTP-запит = 0