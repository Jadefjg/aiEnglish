-- In-app notifications + WeChat openid for parent push
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

SET @col := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='users' AND column_name='wechat_openid');
SET @sql := IF(@col=0, 'ALTER TABLE users ADD COLUMN wechat_openid VARCHAR(64) NULL', 'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @col := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='users' AND column_name='notify_webhook');
SET @sql := IF(@col=0, 'ALTER TABLE users ADD COLUMN notify_webhook VARCHAR(500) NULL', 'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

INSERT INTO schema_migrations(version) VALUES ('005_notify_storage')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
