-- 1v1 slot completed status + audio resource types for listen/dictation

ALTER TABLE tutor_slots
  MODIFY COLUMN status ENUM('open','booked','cancelled','completed') NOT NULL DEFAULT 'open';

ALTER TABLE resources
  MODIFY COLUMN type ENUM('mp4','pdf','mp3','wav','audio') NOT NULL;

INSERT INTO schema_migrations(version) VALUES ('010_tutor_completed_audio')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
