CREATE DATABASE IF NOT EXISTS aienglish CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE aienglish;
CREATE TABLE IF NOT EXISTS features (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, role ENUM('student','teacher') NOT NULL,
  title VARCHAR(200) NOT NULL, description TEXT, manual_pages VARCHAR(20), sort_order INT NOT NULL DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS resources (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, title VARCHAR(200) NOT NULL,
  role ENUM('student','teacher','general') NOT NULL DEFAULT 'general', type ENUM('mp4','pdf') NOT NULL,
  filename VARCHAR(255) NOT NULL UNIQUE, url VARCHAR(500) NOT NULL, duration_seconds INT, size_bytes BIGINT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS feature_resources (
  feature_id BIGINT NOT NULL, resource_id BIGINT NOT NULL, PRIMARY KEY(feature_id,resource_id),
  FOREIGN KEY(feature_id) REFERENCES features(id) ON DELETE CASCADE,
  FOREIGN KEY(resource_id) REFERENCES resources(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, username VARCHAR(50) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL, display_name VARCHAR(100) NOT NULL,
  role ENUM('student','teacher','parent','admin') NOT NULL DEFAULT 'student',
  parent_invite_code VARCHAR(16) NULL,
  wechat_openid VARCHAR(64) NULL,
  notify_webhook VARCHAR(500) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_users_parent_invite(parent_invite_code)
);
CREATE TABLE IF NOT EXISTS parent_student_links (
  parent_id BIGINT NOT NULL, student_id BIGINT NOT NULL,
  status ENUM('active','revoked') NOT NULL DEFAULT 'active',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(parent_id, student_id),
  FOREIGN KEY(parent_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS learning_progress (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, user_id BIGINT NOT NULL, feature_id BIGINT NOT NULL,
  progress TINYINT UNSIGNED NOT NULL DEFAULT 0, completed_at TIMESTAMP NULL,
  UNIQUE KEY uk_user_feature(user_id,feature_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(feature_id) REFERENCES features(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS words (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, word VARCHAR(100) NOT NULL, phonetic VARCHAR(100) NULL,
  meaning VARCHAR(255) NOT NULL, example_sentence VARCHAR(500) NULL, created_by BIGINT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE KEY uk_word(word)
);
CREATE TABLE IF NOT EXISTS questions (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, stem TEXT NOT NULL,
  option_a VARCHAR(255) NOT NULL, option_b VARCHAR(255) NOT NULL, option_c VARCHAR(255) NOT NULL, option_d VARCHAR(255) NOT NULL,
  answer CHAR(1) NOT NULL, explanation VARCHAR(500) NULL, created_by BIGINT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS classes (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(120) NOT NULL, description VARCHAR(500) NULL,
  teacher_id BIGINT NOT NULL, invite_code VARCHAR(16) NOT NULL,
  status ENUM('active','archived') NOT NULL DEFAULT 'active',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE KEY uk_invite_code(invite_code),
  FOREIGN KEY(teacher_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS class_members (
  class_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
  member_role ENUM('student','assistant') NOT NULL DEFAULT 'student',
  joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(class_id, user_id),
  FOREIGN KEY(class_id) REFERENCES classes(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS assignments (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, title VARCHAR(200) NOT NULL, description TEXT NULL,
  teacher_id BIGINT NOT NULL, class_id BIGINT NULL, status ENUM('draft','published','closed') NOT NULL DEFAULT 'draft',
  due_at DATETIME NULL, last_urged_at TIMESTAMP NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, published_at TIMESTAMP NULL,
  FOREIGN KEY(teacher_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS pronunciation_attempts (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, user_id BIGINT NOT NULL, assignment_id BIGINT NULL, task_id BIGINT NULL,
  reference_text VARCHAR(1000) NOT NULL, audio_url VARCHAR(500) NOT NULL,
  overall_score TINYINT UNSIGNED NOT NULL DEFAULT 0, accuracy_score TINYINT UNSIGNED NULL,
  fluency_score TINYINT UNSIGNED NULL, completeness_score TINYINT UNSIGNED NULL, prosody_score TINYINT UNSIGNED NULL,
  recognized_text VARCHAR(1000) NULL, word_feedback_json JSON NULL, provider VARCHAR(40) NOT NULL DEFAULT 'local',
  tips VARCHAR(500) NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS assignment_tasks (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, assignment_id BIGINT NOT NULL,
  task_type ENUM('choice','video','vocab','voice','listen','dictation') NOT NULL, title VARCHAR(200) NOT NULL,
  sort_order INT NOT NULL DEFAULT 0, config_json JSON NOT NULL,
  FOREIGN KEY(assignment_id) REFERENCES assignments(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS assignment_students (
  assignment_id BIGINT NOT NULL, student_id BIGINT NOT NULL,
  urged_at TIMESTAMP NULL, urge_count INT NOT NULL DEFAULT 0,
  PRIMARY KEY(assignment_id, student_id),
  FOREIGN KEY(assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS assignment_submissions (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, assignment_id BIGINT NOT NULL, student_id BIGINT NOT NULL,
  status ENUM('in_progress','submitted','reviewed') NOT NULL DEFAULT 'in_progress',
  score TINYINT UNSIGNED NULL, teacher_comment VARCHAR(500) NULL, submitted_at TIMESTAMP NULL,
  UNIQUE KEY uk_assign_student(assignment_id, student_id),
  FOREIGN KEY(assignment_id) REFERENCES assignments(id) ON DELETE CASCADE,
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_answers (
  id BIGINT PRIMARY KEY AUTO_INCREMENT, submission_id BIGINT NOT NULL, task_id BIGINT NOT NULL,
  answer_json JSON NOT NULL, is_correct TINYINT NULL, score TINYINT UNSIGNED NULL, completed_at TIMESTAMP NULL,
  UNIQUE KEY uk_sub_task(submission_id, task_id),
  FOREIGN KEY(submission_id) REFERENCES assignment_submissions(id) ON DELETE CASCADE,
  FOREIGN KEY(task_id) REFERENCES assignment_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS vocab_progress (
  user_id BIGINT NOT NULL, word_id BIGINT NOT NULL, mastery TINYINT UNSIGNED NOT NULL DEFAULT 0,
  last_reviewed_at TIMESTAMP NULL, PRIMARY KEY(user_id, word_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(word_id) REFERENCES words(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS notifications (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  title VARCHAR(200) NOT NULL,
  body VARCHAR(1000) NOT NULL,
  category VARCHAR(40) NOT NULL DEFAULT 'general',
  link_url VARCHAR(300) NULL,
  payload_json JSON NULL,
  channel_status VARCHAR(200) NOT NULL DEFAULT 'inbox',
  is_read TINYINT NOT NULL DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_notifications_user(user_id, is_read, id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
