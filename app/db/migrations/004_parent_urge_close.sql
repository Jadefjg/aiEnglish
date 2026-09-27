-- Parent-child binding, homework urge, due-date auto-close support columns

SET @col := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='users' AND column_name='parent_invite_code');
SET @sql := IF(@col=0, 'ALTER TABLE users ADD COLUMN parent_invite_code VARCHAR(16) NULL, ADD UNIQUE KEY uk_users_parent_invite(parent_invite_code)', 'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS parent_student_links (
  parent_id BIGINT NOT NULL,
  student_id BIGINT NOT NULL,
  status ENUM('active','revoked') NOT NULL DEFAULT 'active',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(parent_id, student_id),
  KEY idx_psl_student(student_id),
  FOREIGN KEY(parent_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
);

SET @col := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='assignments' AND column_name='last_urged_at');
SET @sql := IF(@col=0, 'ALTER TABLE assignments ADD COLUMN last_urged_at TIMESTAMP NULL', 'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='assignment_students' AND column_name='urged_at');
SET @sql := IF(@col=0, 'ALTER TABLE assignment_students ADD COLUMN urged_at TIMESTAMP NULL, ADD COLUMN urge_count INT NOT NULL DEFAULT 0', 'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

INSERT INTO schema_migrations(version) VALUES ('004_parent_urge_close')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
