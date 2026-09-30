-- Widen notification channel_status if an older 005 created VARCHAR(40)
SET @col_type := (
  SELECT DATA_TYPE FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='notifications' AND column_name='channel_status'
);
SET @char_len := (
  SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns
  WHERE table_schema=DATABASE() AND table_name='notifications' AND column_name='channel_status'
);
SET @sql := IF(
  @col_type IS NOT NULL AND (@char_len IS NULL OR @char_len < 200),
  'ALTER TABLE notifications MODIFY COLUMN channel_status VARCHAR(200) NOT NULL DEFAULT ''inbox''',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Helpful index for parent-student lookups by student
SET @idx := (
  SELECT COUNT(*) FROM information_schema.statistics
  WHERE table_schema=DATABASE() AND table_name='parent_student_links' AND index_name='idx_psl_student'
);
SET @sql := IF(@idx=0, 'CREATE INDEX idx_psl_student ON parent_student_links(student_id, status)', 'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

INSERT INTO schema_migrations(version) VALUES ('006_fixups')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
