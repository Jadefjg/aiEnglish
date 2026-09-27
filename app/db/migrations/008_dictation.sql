-- Dictation (听写) homework task type

ALTER TABLE assignment_tasks
  MODIFY COLUMN task_type ENUM('choice','video','vocab','voice','listen','dictation') NOT NULL;

INSERT INTO schema_migrations(version) VALUES ('008_dictation')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
