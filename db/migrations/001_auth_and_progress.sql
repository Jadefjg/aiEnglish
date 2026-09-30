-- Idempotent migration for existing aienglish installations (MySQL 8.0+).
-- Catalog tables required by learning_progress FK (also created by schema.sql on first init).
CREATE TABLE IF NOT EXISTS features (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  role ENUM('student','teacher') NOT NULL,
  title VARCHAR(200) NOT NULL,
  description TEXT,
  manual_pages VARCHAR(20),
  sort_order INT NOT NULL DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS resources (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  title VARCHAR(200) NOT NULL,
  role ENUM('student','teacher','general') NOT NULL DEFAULT 'general',
  type ENUM('mp4','pdf','mp3','wav','m4a') NOT NULL DEFAULT 'mp4',
  filename VARCHAR(255) NOT NULL,
  url VARCHAR(500) NOT NULL,
  duration_seconds INT,
  size_bytes BIGINT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_resources_filename(filename)
);
CREATE TABLE IF NOT EXISTS feature_resources (
  feature_id BIGINT NOT NULL,
  resource_id BIGINT NOT NULL,
  PRIMARY KEY(feature_id, resource_id),
  FOREIGN KEY(feature_id) REFERENCES features(id) ON DELETE CASCADE,
  FOREIGN KEY(resource_id) REFERENCES resources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  username VARCHAR(50) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  display_name VARCHAR(100) NOT NULL,
  role ENUM('student','teacher','parent','admin') NOT NULL DEFAULT 'student',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- NOTE: do not use "ADD COLUMN IF NOT EXISTS" (MariaDB-only; breaks MySQL 8.4).
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
