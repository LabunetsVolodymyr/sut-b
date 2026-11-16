import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8080';
const TYPE = __ENV.TEST_TYPE || 'baseline';
const JSON_HEADERS = { headers: { 'Content-Type': 'application/json' } };

// Три профілі навантаження. Thresholds = критерії прийняття
const profiles = {
  // baseline: мінімальне навантаження, 1 віртуальний користувач
  baseline: {
    scenarios: {
      baseline: { executor: 'constant-vus', vus: 1, duration: '30s' },
    },
    thresholds: {
      http_req_failed: ['rate<0.01'],                    // помилок менше 1%
      http_req_duration: ['p(95)<500', 'avg<300'],       // p95 < 500 мс, середній < 300 мс
    },
  },
  // load: очікуване робоче навантаження, 20 користувачів
  load: {
    scenarios: {
      load: {
        executor: 'ramping-vus',
        startVUs: 0,
        stages: [
          { duration: '20s', target: 20 },   // розгін
          { duration: '1m', target: 20 },    // стабільне навантаження
          { duration: '10s', target: 0 },    // спад
        ],
      },
    },
    thresholds: {
      http_req_failed: ['rate<0.01'],
      http_req_duration: ['p(95)<500', 'avg<300'],
    },
  },
  // stress: навантаження поступово зростає за межі норми
  stress: {
    scenarios: {
      stress: {
        executor: 'ramping-vus',
        startVUs: 0,
        stages: [
          { duration: '20s', target: 25 },
          { duration: '20s', target: 50 },
          { duration: '20s', target: 100 },
          { duration: '20s', target: 150 },
          { duration: '10s', target: 0 },
        ],
      },
    },
    thresholds: {
      http_req_failed: ['rate<0.05'],                    // для stress допускаємо до 5%
      http_req_duration: ['p(95)<1500'],
    },
  },
};

export const options = profiles[TYPE];

// Один прохід користувача: 4 пов'язані запити
export default function () {
  // Унікальний суфікс, бо code і reference унікальні в БД
  const sfx = `${__VU}-${__ITER}-${Date.now()}`;

  // 1. POST /students
  const s = http.post(`${BASE}/students`, JSON.stringify(
    { code: `S-${sfx}`, age: 25, entry_score: 70, regular: false }),
    { ...JSON_HEADERS, tags: { name: 'POST /students' } });
  if (!check(s, { 'student 201': (r) => r.status === 201 })) return;
  const studentId = s.json('id');            // dynamic data: id з відповіді

  // 2. POST /courses
  const c = http.post(`${BASE}/courses`, JSON.stringify(
    { code: `C-${sfx}`, price: 100000, capacity: 10, level: 1 }),
    { ...JSON_HEADERS, tags: { name: 'POST /courses' } });
  if (!check(c, { 'course 201': (r) => r.status === 201 })) return;
  const courseId = c.json('id');

  // 3. POST /enrollments: використовує id з попередніх відповідей
  const e = http.post(`${BASE}/enrollments`, JSON.stringify(
    { reference: `E-${sfx}`, student_id: studentId, course_id: courseId, coupon_valid: false }),
    { ...JSON_HEADERS, tags: { name: 'POST /enrollments' } });
  if (!check(e, { 'enrollment 201': (r) => r.status === 201 })) return;
  const enrollmentId = e.json('id');

  // 4. GET /enrollments/{id}: id із відповіді кроку 3
  const g = http.get(`${BASE}/enrollments/${enrollmentId}`,
    { tags: { name: 'GET /enrollments/{id}' } });
  check(g, {
    'get 200': (r) => r.status === 200,
    'status Enrolled': (r) => r.json('status') === 'Enrolled',
  });

  sleep(TYPE === 'stress' ? 0.1 : 0.5);      // пауза користувача (think time)
}