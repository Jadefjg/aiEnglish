-- Idempotent migration for existing aienglish installations (MySQL 8.0+).
CREATE TABLE IF NOT EXISTS users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  username VARCHAR(50) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  display_name VARCHAR(100) NOT NULL,
  role ENUM('student','teacher','parent','admin') NOT NULL DEFAULT 'student',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(50) NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255) NULL;
SET @idx_exists := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='users' AND index_name='uk_users_username');
SET @idx_sql := IF(@idx_exists=0, 'CREATE UNIQUE INDEX uk_users_username ON users(username)', 'SELECT 1');
PREPARE idx_stmt FROM @idx_sql;
EXECUTE idx_stmt;
DEALLOCATE PREPARE idx_stmt;
CREATE TABLE IF NOT EXISTS learning_progress (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL, feature_id BIGINT NOT NULL,
  progress TINYINT UNSIGNED NOT NULL DEFAULT 0, completed_at TIMESTAMP NULL,
  UNIQUE KEY uk_user_feature(user_id,feature_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(feature_id) REFERENCES features(id) ON DELETE CASCADE
);
INSERT INTO schema_migrations(version) VALUES ('001_auth_and_progress')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
