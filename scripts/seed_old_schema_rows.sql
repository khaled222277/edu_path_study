-- Realistic rows for the schema as it was at revision 13c704704310 (before sources, feedback and
-- the logout denylist). Used to rehearse `alembic upgrade head` / `downgrade` on a THROWAWAY
-- Postgres only (see tests/test_postgres_migrations.py). Run with psql, UTF-8.
INSERT INTO universities (id, name, country) VALUES (1, 'Al-Baath University - Homs', 'Syria');
INSERT INTO subjects (id, university_id, year, name, code) VALUES
  (7, 1, 5, 'Knowledge Discovery in Databases', 'KDD'),
  (5, 1, 5, 'قواعد المعطيات', 'DB501');
INSERT INTO users (id, google_id, email, name, university_id, year) VALUES
  (1, 'g-1', 'student.one@example.com', 'Student One', 1, 5),
  (2, 'g-2', 'student.two@example.com', 'طالب اثنان', 1, 5),
  (3, 'g-3', 'new.user@example.com', 'No Onboarding', NULL, NULL);
INSERT INTO chat_sessions (id, user_id, subject_id) VALUES (1, 1, 7), (2, 2, 7), (3, 2, 5);
INSERT INTO chat_messages (id, session_id, role, content) VALUES
  (1, 1, 'user', 'what is data mining'),
  (2, 1, 'assistant', 'Data mining is the process of discovering patterns [kdd_lec01_Introduction.pdf, page 7].'),
  (3, 2, 'user', 'ما هو التنقيب عن البيانات؟'),
  (4, 2, 'assistant', 'التنقيب عن البيانات هو عملية اكتشاف الأنماط [kdd_lec01_Introduction.pdf, page 7] وهي مفيدة.'),
  (5, 3, 'user', 'اشرح مفهوم المفتاح الأساسي'),
  (6, 3, 'assistant', 'المفتاح الأساسي يحدد كل سجل بشكل فريد.');
INSERT INTO quiz_attempts (id, user_id, subject_id, score, total_questions, language, completed_at) VALUES
  (1, 1, 7, 2, 3, 'en', now()),
  (2, 2, 7, 0, 2, 'ar', NULL);
INSERT INTO quiz_items (id, attempt_id, question_text, options_json, correct_answer_index, chosen_answer_index, is_correct, source_citation) VALUES
  (1, 1, 'What does support measure?', '["A ratio","A count","A rule","A tree"]', 0, 0, true,  'kdd_lec04_association_rule.pdf, page 5'),
  (2, 1, 'Which is a boosting method?', '["AdaBoost","k-means","Apriori","PCA"]', 0, 1, false, 'kdd_lec08_classifacation_3.pdf, page 37'),
  (3, 1, 'Confidence is defined as?', '["P(B|A)","P(A)","P(A,B)","1-P(A)"]', 0, 0, true, 'kdd_lec04_association_rule.pdf, page 6'),
  (4, 2, 'ما هو الدعم؟', '["نسبة","عدد","شجرة","قاعدة"]', 0, NULL, NULL, 'kdd_lec04_association_rule.pdf, page 5'),
  (5, 2, 'ما هي الثقة؟', '["احتمال شرطي","عدد","شجرة","قاعدة"]', 0, NULL, NULL, 'kdd_lec04_association_rule.pdf, page 6');
SELECT setval('users_id_seq', 3), setval('chat_sessions_id_seq', 3), setval('chat_messages_id_seq', 6),
       setval('quiz_attempts_id_seq', 2), setval('quiz_items_id_seq', 5), setval('subjects_id_seq', 7), setval('universities_id_seq', 1);
