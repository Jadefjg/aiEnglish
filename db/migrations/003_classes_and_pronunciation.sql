-- Class management + pronunciation assessment history
CREATE TABLE IF NOT EXISTS classes (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(120) NOT NULL,
  description VARCHAR(500) NULL,
  teacher_id BIGINT NOT NULL,
  invite_code VARCHAR(16) NOT NULL,
  status ENUM('active','archived') NOT NULL DEFAULT 'active',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_invite_code(invite_code),
  KEY idx_classes_teacher(teacher_id),
  FOREIGN KEY(teacher_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS class_members (
  class_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  member_role ENUM('student','assistant') NOT NULL DEFAULT 'student',
  joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(class_id, user_id),
  FOREIGN KEY(class_id) REFERENCES classes(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='assignments' AND column_name='class_id'
);
SET @col_sql := IF(@col_exists=0,
  'ALTER TABLE assignments ADD COLUMN class_id BIGINT NULL, ADD KEY idx_assignments_class(class_id)',
  'SELECT 1');
PREPARE col_stmt FROM @col_sql;
EXECUTE col_stmt;
DEALLOCATE PREPARE col_stmt;

CREATE TABLE IF NOT EXISTS pronunciation_attempts (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  assignment_id BIGINT NULL,
  task_id BIGINT NULL,
  reference_text VARCHAR(1000) NOT NULL,
  audio_url VARCHAR(500) NOT NULL,
  overall_score TINYINT UNSIGNED NOT NULL DEFAULT 0,
  accuracy_score TINYINT UNSIGNED NULL,
  fluency_score TINYINT UNSIGNED NULL,
  completeness_score TINYINT UNSIGNED NULL,
  prosody_score TINYINT UNSIGNED NULL,
  recognized_text VARCHAR(1000) NULL,
  word_feedback_json JSON NULL,
  provider VARCHAR(40) NOT NULL DEFAULT 'local',
  tips VARCHAR(500) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_pron_user(user_id),
  KEY idx_pron_assignment(assignment_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

INSERT INTO schema_migrations(version) VALUES ('003_classes_and_pronunciation')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
