"""Unit-тести EnrollmentService.

Без БД, Docker і мережі: репозиторії замінені in-memory двійниками,
що реалізують контракти з courses/ports.py. Тому цей етап CI найшвидший
і запускається першим після build.
"""
from dataclasses import replace
from unittest.mock import Mock

import pytest

from courses.models import Student, Course, Enrollment, EnrollmentRequest, EnrollmentStatus as S
from courses.ports import EnrollmentPolicy
from courses.service import EnrollmentService, EnrollmentRejectedError, ResourceNotFoundError

pytestmark = pytest.mark.unit


# ---------------------------- test doubles ----------------------------------

class InMemoryRepo:
    def __init__(self):
        self.items, self.next_id = {}, 1

    def create(self, item):
        saved = replace(item, id=self.next_id)
        self.items[saved.id] = saved
        self.next_id += 1
        return saved

    def find_by_id(self, item_id):
        return self.items.get(item_id)

    def update(self, item):
        if item.id not in self.items:
            raise ValueError("not found")
        self.items[item.id] = item

    def delete(self, item_id):
        self.items.pop(item_id, None)


class InMemoryEnrollments(InMemoryRepo):
    def find_enrollments(self, course_id, status):
        return sorted((e for e in self.items.values() if e.course_id == course_id and e.status == status),
                      key=lambda e: e.id)

    def count_occupied(self, course_id):
        return sum(1 for e in self.items.values() if e.course_id == course_id and e.status != S.CANCELLED)

    def has_enrollment(self, student_id, course_id):
        return any(e.student_id == student_id and e.course_id == course_id and e.status != S.CANCELLED
                   for e in self.items.values())

    def update_status(self, enrollment_id, next_status, refund):
        e = self.items.get(enrollment_id)
        if e is None or e.status != S.ENROLLED:
            raise EnrollmentRejectedError("not active")
        self.items[enrollment_id] = replace(e, status=next_status, refund=refund)


class Ctx:
    def __init__(self):
        self.students, self.courses, self.enrollments = InMemoryRepo(), InMemoryRepo(), InMemoryEnrollments()
        self.policy = Mock(spec=EnrollmentPolicy)
        self.policy.is_allowed.return_value = True
        self.service = EnrollmentService(self.students, self.courses, self.enrollments, self.policy)
        self.counter = 0

    def student(self, age=25, score=70, regular=False):
        self.counter += 1
        return self.students.create(Student(0, f"S-{self.counter}", age, score, regular))

    def course(self, price=100_000, capacity=10, level=1):
        self.counter += 1
        return self.courses.create(Course(0, f"C-{self.counter}", price, capacity, level))

    def enroll(self, student, course, coupon_valid=False):
        self.counter += 1
        return self.service.enroll(EnrollmentRequest(f"R-{self.counter}", student.id, course.id, coupon_valid))


@pytest.fixture
def ctx():
    return Ctx()


# ---------------------------- calculate_total --------------------------------

@pytest.mark.parametrize("price", [100, 12_345, 10_000_000])
@pytest.mark.parametrize("regular, coupon", [(False, False), (True, False), (False, True), (True, True)])
def test_total_has_no_discount(ctx, price, regular, coupon):
    assert ctx.service.calculate_total(price, regular, coupon) == price


@pytest.mark.parametrize("price", [99, 10_000_001, 0, -1, 100.0, True, "100", None])
def test_total_invalid_price_rejected(ctx, price):
    with pytest.raises(ValueError):
        ctx.service.calculate_total(price)


@pytest.mark.parametrize("flag", [0, 1, "true", None])
def test_total_flags_must_be_bool(ctx, flag):
    with pytest.raises(ValueError):
        ctx.service.calculate_total(1000, regular=flag)
    with pytest.raises(ValueError):
        ctx.service.calculate_total(1000, coupon_valid=flag)


# ------------------------------ is_eligible ----------------------------------

@pytest.mark.parametrize("age, score, level, expected", [
    (14, 0, 1, True),      # рівень 1 — усі
    (100, 100, 1, True),
    (14, 59, 2, False),    # рівень 2 — межа 60 включно
    (14, 60, 2, True),
    (17, 100, 3, False),   # рівень 3 — вік від 18
    (18, 79, 3, False),    #            і бал від 80
    (18, 80, 3, True),
    (100, 100, 3, True),
])
def test_eligibility_by_course_level(ctx, age, score, level, expected):
    assert ctx.service.is_eligible(age, score, level) is expected


@pytest.mark.parametrize("age, score, level", [
    (13, 50, 1), (101, 50, 1), (20, -1, 1), (20, 101, 1), (20, 50, 0), (20, 50, 4), (True, 50, 1),
])
def test_eligibility_invalid_input(ctx, age, score, level):
    with pytest.raises(ValueError):
        ctx.service.is_eligible(age, score, level)


# ---------------------------- calculate_refund -------------------------------

@pytest.mark.parametrize("prepaid, days", [(0, 0), (100_000, 0), (100_000, 7), (10_000_000, 36_500)])
def test_refund_is_full_prepaid(ctx, prepaid, days):
    assert ctx.service.calculate_refund(prepaid, days) == prepaid


@pytest.mark.parametrize("prepaid, days", [(-1, 0), (10_000_001, 0), (100, -1), (100, 36_501), (100, True)])
def test_refund_invalid_input(ctx, prepaid, days):
    with pytest.raises(ValueError):
        ctx.service.calculate_refund(prepaid, days)


# --------------------------------- enroll ------------------------------------

def test_enroll_success_creates_enrolled_with_amounts(ctx):
    s, c = ctx.student(regular=True), ctx.course(price=12_345)
    saved = ctx.enroll(s, c, coupon_valid=True)
    assert (saved.status, saved.total, saved.prepaid, saved.refund) == (S.ENROLLED, 12_345, 12_345, 0)
    assert ctx.enrollments.find_by_id(saved.id) == saved
    ctx.policy.is_allowed.assert_called_once_with(s, c)


def test_enroll_policy_false_rejected_nothing_saved(ctx):
    ctx.policy.is_allowed.return_value = False
    s, c = ctx.student(), ctx.course()
    with pytest.raises(EnrollmentRejectedError):
        ctx.enroll(s, c)
    assert ctx.enrollments.items == {}


@pytest.mark.parametrize("answer", [None, 1, "true"])
def test_enroll_policy_non_bool_answer_value_error(ctx, answer):
    ctx.policy.is_allowed.return_value = answer
    with pytest.raises(ValueError):
        ctx.enroll(ctx.student(), ctx.course())
    assert ctx.enrollments.items == {}


def test_enroll_policy_exception_propagates(ctx):
    ctx.policy.is_allowed.side_effect = ConnectionError("down")
    with pytest.raises(ConnectionError):
        ctx.enroll(ctx.student(), ctx.course())
    assert ctx.enrollments.items == {}


def test_enroll_not_eligible_rejected_policy_not_called(ctx):
    s, c = ctx.student(score=59), ctx.course(level=2)
    with pytest.raises(EnrollmentRejectedError):
        ctx.enroll(s, c)
    ctx.policy.is_allowed.assert_not_called()
    assert ctx.enrollments.items == {}


def test_enroll_no_free_seat_rejected_policy_not_called(ctx):
    c = ctx.course(capacity=1)
    ctx.enroll(ctx.student(), c)
    ctx.policy.reset_mock()
    with pytest.raises(EnrollmentRejectedError):
        ctx.enroll(ctx.student(), c)
    ctx.policy.is_allowed.assert_not_called()
    assert ctx.enrollments.count_occupied(c.id) == 1


def test_enroll_duplicate_rejected(ctx):
    s, c = ctx.student(), ctx.course()
    ctx.enroll(s, c)
    with pytest.raises(EnrollmentRejectedError):
        ctx.enroll(s, c)
    assert len(ctx.enrollments.items) == 1


def test_completed_still_occupies_seat(ctx):
    c = ctx.course(capacity=1)
    ctx.service.complete(ctx.enroll(ctx.student(), c).id)
    with pytest.raises(EnrollmentRejectedError):
        ctx.enroll(ctx.student(), c)


def test_reenroll_after_cancel_allowed(ctx):
    s, c = ctx.student(), ctx.course(capacity=1)
    first = ctx.enroll(s, c)
    ctx.service.cancel(first.id)
    second = ctx.enroll(s, c)
    assert second.id != first.id and second.status == S.ENROLLED


def test_enroll_missing_student_or_course(ctx):
    c = ctx.course()
    with pytest.raises(ResourceNotFoundError):
        ctx.service.enroll(EnrollmentRequest("R-x", 999, c.id))
    s = ctx.student()
    with pytest.raises(ResourceNotFoundError):
        ctx.service.enroll(EnrollmentRequest("R-y", s.id, 999))


@pytest.mark.parametrize("request_", [
    EnrollmentRequest("", 1, 1), EnrollmentRequest("   ", 1, 1), EnrollmentRequest("x" * 41, 1, 1),
    EnrollmentRequest("R", 0, 1), EnrollmentRequest("R", 1, True), EnrollmentRequest("R", 1, 1, 1),
    "not a request",
])
def test_enroll_invalid_request_value_error(ctx, request_):
    with pytest.raises(ValueError):
        ctx.service.enroll(request_)
    ctx.policy.is_allowed.assert_not_called()


# ------------------------------ transitions ----------------------------------

def test_complete_keeps_amounts(ctx):
    saved = ctx.enroll(ctx.student(), ctx.course(price=5_000))
    done = ctx.service.complete(saved.id)
    assert done == replace(saved, status=S.COMPLETED)
    assert ctx.enrollments.find_by_id(saved.id) == done
    ctx.policy.is_allowed.assert_called_once()  # лише під час enroll


def test_cancel_refunds_full_prepaid(ctx):
    saved = ctx.enroll(ctx.student(), ctx.course(price=7_777))
    cancelled = ctx.service.cancel(saved.id, 3)
    assert (cancelled.status, cancelled.total, cancelled.prepaid, cancelled.refund) == (S.CANCELLED, 7_777, 7_777, 7_777)
    assert ctx.enrollments.find_by_id(saved.id) == cancelled


@pytest.mark.parametrize("first, second", [
    ("complete", "complete"), ("complete", "cancel"), ("cancel", "cancel"), ("cancel", "complete"),
])
def test_repeated_transitions_rejected(ctx, first, second):
    saved = ctx.enroll(ctx.student(), ctx.course())
    after_first = getattr(ctx.service, first)(saved.id)
    with pytest.raises(EnrollmentRejectedError):
        getattr(ctx.service, second)(saved.id)
    assert ctx.enrollments.find_by_id(saved.id) == after_first


def test_transition_missing_enrollment(ctx):
    with pytest.raises(ResourceNotFoundError):
        ctx.service.complete(12345)
    with pytest.raises(ValueError):
        ctx.service.cancel(0)


def test_service_requires_all_dependencies():
    with pytest.raises(ValueError):
        EnrollmentService(InMemoryRepo(), InMemoryRepo(), None, Mock())
