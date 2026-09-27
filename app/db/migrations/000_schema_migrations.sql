CREATE TABLE IF NOT EXISTS schema_migrations (
  version VARCHAR(100) PRIMARY KEY,
  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO schema_migrations(version) VALUES ('000_schema_migrations')
  ON DUPLICATE KEY UPDATE version=VALUES(version);
