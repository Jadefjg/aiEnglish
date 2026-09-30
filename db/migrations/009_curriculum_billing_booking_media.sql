-- Curriculum / billing / 1v1 booking / media anti-cheat sessions

CREATE TABLE IF NOT EXISTS textbooks (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  title VARCHAR(200) NOT NULL,
  level VARCHAR(40) NULL,
  description VARCHAR(500) NULL,
  status ENUM('active','archived') NOT NULL DEFAULT 'active',
  created_by BIGINT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS chapters (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  textbook_id BIGINT NOT NULL,
  title VARCHAR(200) NOT NULL,
  objectives VARCHAR(500) NULL,
  sort_order INT NOT NULL DEFAULT 0,
  word_ids_json JSON NULL,
  question_ids_json JSON NULL,
  resource_filenames_json JSON NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_chapters_book(textbook_id, sort_order),
  FOREIGN KEY(textbook_id) REFERENCES textbooks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS class_textbooks (
  class_id BIGINT NOT NULL,
  textbook_id BIGINT NOT NULL,
  assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(class_id, textbook_id),
  FOREIGN KEY(class_id) REFERENCES classes(id) ON DELETE CASCADE,
  FOREIGN KEY(textbook_id) REFERENCES textbooks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tutor_slots (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  teacher_id BIGINT NOT NULL,
  starts_at DATETIME NOT NULL,
  ends_at DATETIME NOT NULL,
  topic VARCHAR(200) NULL,
  status ENUM('open','booked','cancelled') NOT NULL DEFAULT 'open',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_tutor_slots_teacher(teacher_id, starts_at),
  FOREIGN KEY(teacher_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tutor_bookings (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  slot_id BIGINT NOT NULL,
  student_id BIGINT NOT NULL,
  status ENUM('booked','cancelled','completed') NOT NULL DEFAULT 'booked',
  note VARCHAR(300) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_tutor_slot(slot_id),
  KEY idx_tutor_bookings_student(student_id, status),
  FOREIGN KEY(slot_id) REFERENCES tutor_slots(id) ON DELETE CASCADE,
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tuition_contracts (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  student_id BIGINT NOT NULL,
  teacher_id BIGINT NOT NULL,
  title VARCHAR(200) NOT NULL,
  total_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
  paid_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
  currency VARCHAR(8) NOT NULL DEFAULT 'CNY',
  hours_included DECIMAL(8,1) NOT NULL DEFAULT 0,
  hours_granted DECIMAL(8,1) NOT NULL DEFAULT 0,
  status ENUM('draft','active','completed','cancelled') NOT NULL DEFAULT 'draft',
  start_date DATE NULL,
  end_date DATE NULL,
  note VARCHAR(500) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_contracts_student(student_id, status),
  KEY idx_contracts_teacher(teacher_id, status),
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(teacher_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tuition_payments (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  contract_id BIGINT NOT NULL,
  amount DECIMAL(12,2) NOT NULL,
  method ENUM('cash','wechat','alipay','transfer','other') NOT NULL DEFAULT 'transfer',
  paid_at DATETIME NOT NULL,
  hours_granted DECIMAL(8,1) NOT NULL DEFAULT 0,
  note VARCHAR(300) NULL,
  created_by BIGINT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_payments_contract(contract_id, paid_at),
  FOREIGN KEY(contract_id) REFERENCES tuition_contracts(id) ON DELETE CASCADE,
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS media_progress_sessions (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  assignment_id BIGINT NOT NULL,
  task_id BIGINT NOT NULL,
  media_key VARCHAR(300) NOT NULL,
  duration_seconds INT NOT NULL DEFAULT 0,
  max_position DECIMAL(10,2) NOT NULL DEFAULT 0,
  cumulative_watch DECIMAL(10,2) NOT NULL DEFAULT 0,
  token_hash VARCHAR(64) NOT NULL,
  expires_at DATETIME NOT NULL,
  last_heartbeat_at DATETIME NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_media_sess_user(user_id, assignment_id, task_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Ensure task_type enum includes listen + dictation even if 007/008 order differs
ALTER TABLE assignment_tasks
  MODIFY COLUMN task_type ENUM('choice','video','vocab','voice','listen','dictation') NOT NULL;

INSERT INTO schema_migrations(version) VALUES ('009_curriculum_billing_booking_media')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
