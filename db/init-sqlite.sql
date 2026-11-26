-- 文件中转站 SQLite 初始化脚本（根据数据库迁移自动生成，请勿手改）
--
-- 用法：sqlite3 data/app.db < init-sqlite.sql
--
-- 只建表结构并写入迁移版本号；默认设置和管理员 admin/admin 由应用首次启动时写入。
-- 不执行本脚本也可以：应用启动时发现没有数据库会自动创建。
-- 只能用于新文件，已有数据的库请直接启动新版应用，由它自动迁移。

BEGIN;

CREATE TABLE activity_logs (
	id INTEGER NOT NULL,
	user_id INTEGER,
	username VARCHAR(64) NOT NULL,
	action VARCHAR(48) NOT NULL,
	status VARCHAR(16) NOT NULL,
	ip_address VARCHAR(64),
	user_agent TEXT,
	browser VARCHAR(64),
	os_name VARCHAR(64),
	device VARCHAR(32),
	method VARCHAR(8),
	path VARCHAR(512),
	target_type VARCHAR(32),
	target_id VARCHAR(64),
	detail TEXT,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL,
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

CREATE TABLE email_tokens (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	purpose VARCHAR(32) NOT NULL,
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	used_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE "files" (
	id INTEGER NOT NULL,
	public_id VARCHAR(32) NOT NULL,
	user_id INTEGER NOT NULL,
	original_name VARCHAR(255) NOT NULL,
	stored_path VARCHAR(255) NOT NULL,
	size BIGINT NOT NULL,
	mime_type VARCHAR(128) NOT NULL,
	sha256 VARCHAR(64),
	download_count INTEGER NOT NULL,
	is_public BOOLEAN NOT NULL,
	is_deleted BOOLEAN NOT NULL,
	uploaded_ip VARCHAR(64),
	created_at DATETIME NOT NULL,
	expires_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE offline_task_files (
	id INTEGER NOT NULL,
	task_id VARCHAR(32) NOT NULL,
	file_index INTEGER NOT NULL,
	relative_path TEXT NOT NULL,
	name VARCHAR(255) NOT NULL,
	size BIGINT NOT NULL,
	selected BOOLEAN NOT NULL,
	completed_bytes BIGINT NOT NULL,
	stored_path VARCHAR(255),
	public_id VARCHAR(32),
	file_id INTEGER,
	imported_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_offline_file_index UNIQUE (task_id, file_index),
	FOREIGN KEY(task_id) REFERENCES offline_tasks (id) ON DELETE CASCADE,
	UNIQUE (public_id),
	FOREIGN KEY(file_id) REFERENCES files (id) ON DELETE SET NULL
);

CREATE TABLE offline_task_transfers (
	id INTEGER NOT NULL,
	task_id VARCHAR(32) NOT NULL,
	gid VARCHAR(16) NOT NULL,
	parent_gid VARCHAR(16),
	role VARCHAR(16) NOT NULL,
	state VARCHAR(16) NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id) REFERENCES offline_tasks (id) ON DELETE CASCADE,
	UNIQUE (gid)
);

CREATE TABLE offline_tasks (
	id VARCHAR(32) NOT NULL,
	user_id INTEGER NOT NULL,
	request_key VARCHAR(64),
	kind VARCHAR(16) NOT NULL,
	source_encrypted TEXT NOT NULL,
	name VARCHAR(255) NOT NULL,
	info_hash VARCHAR(40),
	state VARCHAR(32) NOT NULL,
	desired_state VARCHAR(16) NOT NULL,
	is_public BOOLEAN NOT NULL,
	expires_hours INTEGER NOT NULL,
	reserved_files INTEGER NOT NULL,
	reserved_bytes BIGINT NOT NULL,
	total_bytes BIGINT NOT NULL,
	completed_bytes BIGINT NOT NULL,
	download_speed BIGINT NOT NULL,
	upload_speed BIGINT NOT NULL,
	connections INTEGER NOT NULL,
	error VARCHAR(512),
	imported_at DATETIME,
	seed_started_at DATETIME,
	cleanup_pending BOOLEAN NOT NULL,
	removed_at DATETIME,
	revision INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL, payload_ready BOOLEAN DEFAULT 0 NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_offline_task_request UNIQUE (user_id, request_key),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE settings (
	"key" VARCHAR(128) NOT NULL,
	value TEXT NOT NULL,
	updated_at DATETIME NOT NULL,
	updated_by INTEGER,
	PRIMARY KEY ("key")
);

CREATE TABLE task_locks (
	name VARCHAR(64) NOT NULL,
	locked_until DATETIME,
	PRIMARY KEY (name)
);

CREATE TABLE upload_sessions (
	id VARCHAR(32) NOT NULL,
	user_id INTEGER NOT NULL,
	name VARCHAR(255) NOT NULL,
	size BIGINT NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	"offset" BIGINT NOT NULL,
	expires_hours INTEGER NOT NULL,
	is_public BOOLEAN NOT NULL,
	stored_path VARCHAR(255) NOT NULL,
	state VARCHAR(16) NOT NULL,
	file_id INTEGER,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(file_id) REFERENCES files (id) ON DELETE SET NULL
);

CREATE TABLE user_sessions (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	csrf_token VARCHAR(64) NOT NULL,
	ip_address VARCHAR(64),
	user_agent TEXT,
	created_at DATETIME NOT NULL,
	last_seen_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	revoked BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE "users" (
	id INTEGER NOT NULL,
	username VARCHAR(64) NOT NULL,
	email VARCHAR(255) NOT NULL,
	password_hash VARCHAR(255) NOT NULL,
	is_admin BOOLEAN NOT NULL,
	is_active BOOLEAN NOT NULL,
	email_verified BOOLEAN NOT NULL,
	must_change_password BOOLEAN NOT NULL,
	quota_max_files INTEGER,
	quota_max_bytes BIGINT,
	used_files INTEGER NOT NULL,
	used_bytes BIGINT NOT NULL,
	created_at DATETIME NOT NULL,
	last_login_at DATETIME,
	last_login_ip VARCHAR(64),
	can_permanent BOOLEAN NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_activity_logs_action ON activity_logs (action);

CREATE INDEX ix_activity_logs_user_id ON activity_logs (user_id);

CREATE INDEX ix_email_tokens_expires_at ON email_tokens (expires_at);

CREATE INDEX ix_email_tokens_purpose ON email_tokens (purpose);

CREATE UNIQUE INDEX ix_email_tokens_token_hash ON email_tokens (token_hash);

CREATE INDEX ix_email_tokens_user_id ON email_tokens (user_id);

CREATE INDEX ix_files_created_at ON files (created_at);

CREATE INDEX ix_files_expires_at ON files (expires_at);

CREATE INDEX ix_files_expires_at_active ON files (expires_at, is_deleted);

CREATE UNIQUE INDEX ix_files_public_id ON files (public_id);

CREATE INDEX ix_files_public_list ON files (is_public, is_deleted, created_at);

CREATE INDEX ix_files_user_expires ON files (user_id, expires_at);

CREATE INDEX ix_files_user_id ON files (user_id);

CREATE INDEX ix_logs_action_created ON activity_logs (action, created_at);

CREATE INDEX ix_logs_created_at ON activity_logs (created_at);

CREATE INDEX ix_logs_user_created ON activity_logs (user_id, created_at);

CREATE INDEX ix_offline_task_files_file_id ON offline_task_files (file_id);

CREATE INDEX ix_offline_tasks_info_hash ON offline_tasks (info_hash);

CREATE INDEX ix_offline_tasks_state ON offline_tasks (state);

CREATE INDEX ix_offline_tasks_user_created ON offline_tasks (user_id, created_at);

CREATE INDEX ix_offline_transfers_task ON offline_task_transfers (task_id);

CREATE INDEX ix_upload_sessions_expires_at ON upload_sessions (expires_at);

CREATE INDEX ix_upload_sessions_state ON upload_sessions (state);

CREATE INDEX ix_upload_sessions_user_id ON upload_sessions (user_id);

CREATE INDEX ix_user_sessions_expires_at ON user_sessions (expires_at);

CREATE UNIQUE INDEX ix_user_sessions_token_hash ON user_sessions (token_hash);

CREATE INDEX ix_user_sessions_user_id ON user_sessions (user_id);

CREATE UNIQUE INDEX ix_users_email ON users (email);

CREATE UNIQUE INDEX ix_users_username ON users (username);

INSERT INTO alembic_version (version_num) VALUES ('a6b9e2037c51');

COMMIT;
