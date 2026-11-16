import uuid
from unittest.mock import Mock

import pytest

from courses.database import connect
from courses.models import (
    Student, Course, Enrollment, EnrollmentRequest, EnrollmentStatus as S,
)
from courses.ports import EnrollmentPolicy
from courses.repositories import (
    PostgresStudentRepository,
    PostgresCourseRepository,
    PostgresEnrollmentRepository,
)
from courses.service import EnrollmentService, EnrollmentRejectedError


def uid() -> str:
    # Унікальний суфікс для code/reference, щоб тести не конфліктували
    # між собою (у БД ці поля унікальні)
    return uuid.uuid4().hex[:12]


class Env:
    """Тестове оточення.

    Реальні: EnrollmentService, три Postgres-репозиторії, реальна БД.
    Test Double (Mock): лише EnrollmentPolicy, бо це зовнішня залежність,
    яка не входить до integration boundary.
    """

    def __init__(self, conn):
        self.students = PostgresStudentRepository(conn)
        self.courses = PostgresCourseRepository(conn)
        self.enrollments = PostgresEnrollmentRepository(conn)
        self.policy = Mock(spec=EnrollmentPolicy)  # Test Double поза integration boundary
        self.policy.is_allowed.return_value = True  # за замовчуванням політика дозволяє
        self.service = EnrollmentService(
            self.students, self.courses, self.enrollments, self.policy)
        # Запам'ятовуємо створені id, щоб cleanup() потім їх видалив
        self.student_ids = []
        self.course_ids = []

    def add_student(self, age=25, score=70, regular=False):
        # Створює слухача в реальній БД; id=0 лише заглушка, справжній id дає БД
        s = self.students.create(Student(0, f"s-{uid()}", age, score, regular))
        self.student_ids.append(s.id)
        return s

    def add_course(self, price=10_000, capacity=5, level=1):
        # Створює курс у реальній БД
        c = self.courses.create(Course(0, f"c-{uid()}", price, capacity, level))
        self.course_ids.append(c.id)
        return c

    def request(self, student, course, coupon_valid=False, ref=None):
        # Збирає запит на зарахування з унікальним reference
        return EnrollmentRequest(ref or f"r-{uid()}", student.id, course.id, coupon_valid)

    def cleanup(self):
        # Очищення після тесту. Порядок важливий через зовнішні ключі:
        # FK: спочатку заявки, потім слухачі та курси
        for course_id in self.course_ids:
            for status in S:
                for e in self.enrollments.find_enrollments(course_id, status):
                    self.enrollments.delete(e.id)
        for sid in self.student_ids:
            self.students.delete(sid)
        for cid in self.course_ids:
            self.courses.delete(cid)


@pytest.fixture
def env():
    # Нове оточення для кожного тесту; з'єднання закривається через with.
    # cleanup() у finally виконується навіть якщо тест упав, тому тести
    # незалежні, а набір можна запускати повторно
    with connect() as conn:
        e = Env(conn)
        try:
            yield e
        finally:
            e.cleanup()


# ===================== Repository <-> Database ===============================

def test_student_save_and_find_repositoryState(env):
    # Зберігаємо слухача через реальний репозиторій (справжній INSERT)
    saved = env.students.create(Student(0, f"s-{uid()}", 30, 85, True))
    env.student_ids.append(saved.id)  # щоб cleanup його видалив

    # Читаємо з БД окремим запитом
    found = env.students.find_by_id(saved.id)

    assert saved.id > 0  # id призначила БД, а не вхідний 0
    assert found == saved  # усі поля збереглися й прочиталися правильно
    assert (found.age, found.entry_score, found.regular) == (30, 85, True)
    # Неіснуючий id -> None, а не помилка
    assert env.students.find_by_id(saved.id + 10_000_000) is None


def test_enrollment_save_and_find_repositoryState(env):
    # Заявка має зовнішні ключі, тому спершу створюємо слухача й курс
    s, c = env.add_student(), env.add_course()
    saved = env.enrollments.create(
        Enrollment(0, f"r-{uid()}", s.id, c.id, 1000, 1000, 0, S.ENROLLED))

    assert saved.id > 0  # id видала БД
    # Читання з БД збігається зі збереженим: перевіряє INSERT, схему й mapping
    assert env.enrollments.find_by_id(saved.id) == saved
    # Пошук за курсом і статусом: Enrolled -> є, Cancelled -> порожньо
    assert env.enrollments.find_enrollments(c.id, S.ENROLLED) == [saved]
    assert env.enrollments.find_enrollments(c.id, S.CANCELLED) == []
    # Enrolled займає одне місце, а слухач має активну заявку на цей курс
    assert env.enrollments.count_occupied(c.id) == 1
    assert env.enrollments.has_enrollment(s.id, c.id) is True


def test_enrollment_update_status_and_delete_repositoryChange(env):
    s, c = env.add_student(), env.add_course()
    saved = env.enrollments.create(
        Enrollment(0, f"r-{uid()}", s.id, c.id, 1000, 1000, 0, S.ENROLLED))

    # Змінюємо статус на Cancelled з поверненням 1000
    env.enrollments.update_status(saved.id, S.CANCELLED, 1000)

    # Перевіряємо фактичний стан у БД після зміни
    after = env.enrollments.find_by_id(saved.id)
    assert after.status == S.CANCELLED and after.refund == 1000
    assert env.enrollments.count_occupied(c.id) == 0  # Cancelled місця не займає

    # Видалення: після delete запису в БД немає
    env.enrollments.delete(saved.id)
    assert env.enrollments.find_by_id(saved.id) is None


# ================= Service -> Repository -> Database =========================

def test_enroll_success_serviceState(env):
    # Позитивний сценарій. regular=True і купон не мають змінювати суму,
    # бо персональне правило варіанта знижок не передбачає
    s = env.add_student(regular=True)  # regular не впливає на суму
    c = env.add_course(price=12_345, level=1)

    result = env.service.enroll(env.request(s, c, coupon_valid=True))

    # Перевіряємо не лише результат сервісу, а й фактичний стан БД
    stored = env.enrollments.find_enrollments(c.id, S.ENROLLED)
    assert stored == [result]  # у БД рівно одна заявка, вона збігається з поверненою
    assert stored[0].student_id == s.id and stored[0].course_id == c.id
    # total = ціна, prepaid = total, refund = 0
    assert (stored[0].total, stored[0].prepaid, stored[0].refund) == (12_345, 12_345, 0)
    # Зовнішню політику викликано один раз із потрібними аргументами
    env.policy.is_allowed.assert_called_once_with(s, c)


def test_enroll_policy_denied_serviceState(env):
    # Негативний сценарій: зовнішня політика забороняє зарахування
    s, c = env.add_student(), env.add_course()
    env.policy.is_allowed.return_value = False

    with pytest.raises(EnrollmentRejectedError):
        env.service.enroll(env.request(s, c))

    # Заявку не створено: БД не змінилась
    assert env.enrollments.has_enrollment(s.id, c.id) is False
    assert env.enrollments.count_occupied(c.id) == 0
    for status in S:
        assert env.enrollments.find_enrollments(c.id, status) == []
    # Політика була викликана один раз (локальні перевірки пройшли)
    env.policy.is_allowed.assert_called_once_with(s, c)


# =============== Персональне правило: допуск за рівнем курсу =================
# Рівень 1: усі; рівень 2: бал від 60; рівень 3: вік від 18 і бал від 80.
# Усі пороги включні, тому перевіряємо межі.

def test_level2_score_60_allowed(env):
    # Межа включна: рівно 60 балів допускається
    s, c = env.add_student(score=60), env.add_course(level=2)

    env.service.enroll(env.request(s, c))

    assert env.enrollments.has_enrollment(s.id, c.id) is True


def test_level2_score_59_rejected_no_record_no_policy_call(env):
    # 59 балів для рівня 2: відмова
    s, c = env.add_student(score=59), env.add_course(level=2)

    with pytest.raises(EnrollmentRejectedError):
        env.service.enroll(env.request(s, c))

    assert env.enrollments.has_enrollment(s.id, c.id) is False  # запису немає
    env.policy.is_allowed.assert_not_called()  # локальна відмова


def test_level3_age_17_with_high_score_rejected(env):
    # Для рівня 3 потрібні вік і бал одночасно: високий бал не компенсує вік 17
    s, c = env.add_student(age=17, score=95), env.add_course(level=3)

    with pytest.raises(EnrollmentRejectedError):
        env.service.enroll(env.request(s, c))

    assert env.enrollments.count_occupied(c.id) == 0
    env.policy.is_allowed.assert_not_called()


def test_level3_age_18_score_80_allowed(env):
    # Обидві межі включні: рівно 18 років і 80 балів допускаються
    s, c = env.add_student(age=18, score=80), env.add_course(level=3)

    env.service.enroll(env.request(s, c))

    assert env.enrollments.count_occupied(c.id) == 1


def test_level3_score_79_rejected(env):
    # Вік підходить, але 79 балів < 80: відмова
    s, c = env.add_student(age=30, score=79), env.add_course(level=3)

    with pytest.raises(EnrollmentRejectedError):
        env.service.enroll(env.request(s, c))

    assert env.enrollments.has_enrollment(s.id, c.id) is False


# ================= Місця, переходи, альтернативні сценарії ===================

def test_no_free_seat_rejected_and_policy_not_called(env):
    s1, s2 = env.add_student(), env.add_student()
    c = env.add_course(capacity=1)  # лише одне місце
    env.service.enroll(env.request(s1, c))  # перший слухач займає його
    env.policy.is_allowed.reset_mock()  # обнуляємо лічильник викликів політики

    # Другий слухач: місць немає -> відмова
    with pytest.raises(EnrollmentRejectedError):
        env.service.enroll(env.request(s2, c))

    assert env.enrollments.count_occupied(c.id) == 1  # лишилась тільки одна заявка
    assert env.enrollments.has_enrollment(s2.id, c.id) is False
    env.policy.is_allowed.assert_not_called()  # локальна відмова, політика не викликана


def test_reenroll_after_cancel_with_new_reference(env):
    s, c = env.add_student(), env.add_course(capacity=1)
    first = env.service.enroll(env.request(s, c))
    env.service.cancel(first.id)  # після скасування місце вільне

    env.service.enroll(env.request(s, c))  # нова заявка, новий reference

    # У БД одна скасована і одна активна заявка
    assert len(env.enrollments.find_enrollments(c.id, S.CANCELLED)) == 1
    assert len(env.enrollments.find_enrollments(c.id, S.ENROLLED)) == 1


def test_cancel_completed_rejected_state_unchanged(env):
    s, c = env.add_student(), env.add_course()
    enrolled = env.service.enroll(env.request(s, c))
    env.service.complete(enrolled.id)  # Enrolled -> Completed

    # Скасувати завершену заявку заборонено
    with pytest.raises(EnrollmentRejectedError):
        env.service.cancel(enrolled.id)

    # Стан у БД не змінився: усе ще Completed, повернення 0
    after = env.enrollments.find_by_id(enrolled.id)
    assert after.status == S.COMPLETED and after.refund == 0


def test_policy_exception_propagates_and_nothing_saved(env):
    s, c = env.add_student(), env.add_course()
    env.policy.is_allowed.side_effect = RuntimeError("policy down")  # політика падає

    # Виняток залежності передається виклику без змін
    with pytest.raises(RuntimeError):
        env.service.enroll(env.request(s, c))

    # Заявку не створено
    assert env.enrollments.has_enrollment(s.id, c.id) is False