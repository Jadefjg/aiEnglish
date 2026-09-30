-- Homework / practice: choice, video, vocab, voice
CREATE TABLE IF NOT EXISTS words (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  word VARCHAR(100) NOT NULL,
  phonetic VARCHAR(100) NULL,
  meaning VARCHAR(255) NOT NULL,
  example_sentence VARCHAR(500) NULL,
  created_by BIGINT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_word(word)
);

CREATE TABLE IF NOT EXISTS questions (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  stem TEXT NOT NULL,
  option_a VARCHAR(255) NOT NULL,
  option_b VARCHAR(255) NOT NULL,
  option_c VARCHAR(255) NOT NULL,
  option_d VARCHAR(255) NOT NULL,
  answer CHAR(1) NOT NULL,
  explanation VARCHAR(500) NULL,
  created_by BIGINT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT chk_answer CHECK (answer IN ('A','B','C','D'))
);

CREATE TABLE IF NOT EXISTS assignments (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  title VARCHAR(200) NOT NULL,
  description TEXT NULL,
  teacher_id BIGINT NOT NULL,
  status ENUM('draft','published','closed') NOT NULL DEFAULT 'draft',
  due_at DATETIME NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  published_at TIMESTAMP NULL,
  FOREIGN KEY(teacher_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assignment_tasks (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  assignment_id BIGINT NOT NULL,
  task_type ENUM('choice','video','vocab','voice') NOT NULL,
  title VARCHAR(200) NOT NULL,
  sort_order INT NOT NULL DEFAULT 0,
  config_json JSON NOT NULL,
  FOREIGN KEY(assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
  KEY idx_assignment_tasks_assignment(assignment_id)
);

CREATE TABLE IF NOT EXISTS assignment_students (
  assignment_id BIGINT NOT NULL,
  student_id BIGINT NOT NULL,
  PRIMARY KEY(assignment_id, student_id),
  FOREIGN KEY(assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assignment_submissions (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  assignment_id BIGINT NOT NULL,
  student_id BIGINT NOT NULL,
  status ENUM('in_progress','submitted','reviewed') NOT NULL DEFAULT 'in_progress',
  score TINYINT UNSIGNED NULL,
  teacher_comment VARCHAR(500) NULL,
  submitted_at TIMESTAMP NULL,
  UNIQUE KEY uk_assign_student(assignment_id, student_id),
  FOREIGN KEY(assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_answers (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  submission_id BIGINT NOT NULL,
  task_id BIGINT NOT NULL,
  answer_json JSON NOT NULL,
  is_correct TINYINT NULL,
  score TINYINT UNSIGNED NULL,
  completed_at TIMESTAMP NULL,
  UNIQUE KEY uk_sub_task(submission_id, task_id),
  FOREIGN KEY(submission_id) REFERENCES assignment_submissions(id) ON DELETE CASCADE,
  FOREIGN KEY(task_id) REFERENCES assignment_tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS vocab_progress (
  user_id BIGINT NOT NULL,
  word_id BIGINT NOT NULL,
  mastery TINYINT UNSIGNED NOT NULL DEFAULT 0,
  last_reviewed_at TIMESTAMP NULL,
  PRIMARY KEY(user_id, word_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(word_id) REFERENCES words(id) ON DELETE CASCADE
);

INSERT INTO words(id, word, phonetic, meaning, example_sentence) VALUES
(1, 'apple', '/ˈæpl/', '苹果', 'I eat an apple every day.'),
(2, 'book', '/bʊk/', '书', 'This book is interesting.'),
(3, 'school', '/skuːl/', '学校', 'We go to school on Monday.'),
(4, 'friend', '/frend/', '朋友', 'She is my best friend.'),
(5, 'happy', '/ˈhæpi/', '开心的', 'I feel happy today.'),
(6, 'water', '/ˈwɔːtə/', '水', 'Please drink more water.'),
(7, 'family', '/ˈfæməli/', '家庭', 'My family loves traveling.'),
(8, 'learn', '/lɜːn/', '学习', 'Children learn English at school.')
ON DUPLICATE KEY UPDATE meaning=VALUES(meaning), phonetic=VALUES(phonetic), example_sentence=VALUES(example_sentence);

INSERT INTO questions(id, stem, option_a, option_b, option_c, option_d, answer, explanation) VALUES
(1, 'What is the plural form of "child"?', 'childs', 'children', 'childes', 'childen', 'B', 'child 的复数是不规则变化 children。'),
(2, 'Choose the correct sentence.', 'He go to school.', 'He goes to school.', 'He going to school.', 'He gone to school.', 'B', '第三人称单数一般现在时动词加 s/es。'),
(3, '"Apple" means ______ in Chinese.', '香蕉', '苹果', '橙子', '葡萄', 'B', 'apple = 苹果。'),
(4, 'Which word is a verb?', 'happy', 'school', 'learn', 'water', 'C', 'learn 是动词「学习」。'),
(5, 'I ______ an apple every morning.', 'eat', 'eats', 'eating', 'ate', 'A', '主语 I 用原形 eat。')
ON DUPLICATE KEY UPDATE stem=VALUES(stem), option_a=VALUES(option_a), option_b=VALUES(option_b),
  option_c=VALUES(option_c), option_d=VALUES(option_d), answer=VALUES(answer), explanation=VALUES(explanation);

INSERT INTO schema_migrations(version) VALUES ('002_homework')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
