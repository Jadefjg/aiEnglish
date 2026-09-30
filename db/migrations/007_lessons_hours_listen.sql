-- Lessons schedule, class-hour packages, listening task type

CREATE TABLE IF NOT EXISTS lessons (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  class_id BIGINT NOT NULL,
  teacher_id BIGINT NOT NULL,
  title VARCHAR(200) NOT NULL,
  topic VARCHAR(500) NULL,
  starts_at DATETIME NOT NULL,
  duration_minutes INT NOT NULL DEFAULT 60,
  status ENUM('scheduled','completed','cancelled') NOT NULL DEFAULT 'scheduled',
  hours_cost DECIMAL(6,1) NOT NULL DEFAULT 1.0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_lessons_class_time(class_id, starts_at),
  KEY idx_lessons_teacher_time(teacher_id, starts_at),
  FOREIGN KEY(class_id) REFERENCES classes(id) ON DELETE CASCADE,
  FOREIGN KEY(teacher_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lesson_attendance (
  lesson_id BIGINT NOT NULL,
  student_id BIGINT NOT NULL,
  status ENUM('present','absent','leave') NOT NULL DEFAULT 'present',
  hours_deducted DECIMAL(6,1) NOT NULL DEFAULT 0,
  note VARCHAR(200) NULL,
  marked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(lesson_id, student_id),
  FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE,
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS student_hour_packages (
  student_id BIGINT PRIMARY KEY,
  total_hours DECIMAL(8,1) NOT NULL DEFAULT 0,
  used_hours DECIMAL(8,1) NOT NULL DEFAULT 0,
  note VARCHAR(200) NULL,
  created_by BIGINT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hour_transactions (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  student_id BIGINT NOT NULL,
  delta_hours DECIMAL(8,1) NOT NULL,
  reason VARCHAR(40) NOT NULL,
  ref_lesson_id BIGINT NULL,
  note VARCHAR(200) NULL,
  created_by BIGINT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_hour_tx_student(student_id, id),
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Expand homework task types with listening
ALTER TABLE assignment_tasks
  MODIFY COLUMN task_type ENUM('choice','video','vocab','voice','listen') NOT NULL;

INSERT INTO schema_migrations(version) VALUES ('007_lessons_hours_listen')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
