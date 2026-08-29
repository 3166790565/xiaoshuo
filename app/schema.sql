-- 小说站数据结构。正文只存 chapters.content 一份，
-- FTS5 用 external content 表指向它，避免正文双写导致库体积翻倍。

CREATE TABLE IF NOT EXISTS books (
  id              INTEGER PRIMARY KEY,
  title           TEXT NOT NULL,
  author          TEXT DEFAULT '未知',
  intro           TEXT DEFAULT '',
  cover_url       TEXT DEFAULT '',
  category        TEXT DEFAULT '',
  word_count      INTEGER DEFAULT 0,
  chapter_count   INTEGER DEFAULT 0,
  split_mode      TEXT DEFAULT 'marked',   -- marked | single | auto
  source_filename TEXT,
  content_hash    TEXT UNIQUE,
  created_at      TEXT,
  updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_books_updated ON books(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_books_split_mode ON books(split_mode);
CREATE INDEX IF NOT EXISTS idx_books_title ON books(title);

CREATE TABLE IF NOT EXISTS chapters (
  id         INTEGER PRIMARY KEY,
  book_id    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  idx        INTEGER NOT NULL,
  title      TEXT NOT NULL,
  content    TEXT NOT NULL,
  word_count INTEGER DEFAULT 0,
  UNIQUE(book_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id, idx);

-- trigram 分词让中文子串检索可用，无需外挂分词器
CREATE VIRTUAL TABLE IF NOT EXISTS chapters_fts USING fts5(
  title, content, content='chapters', content_rowid='id', tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS chapters_ai AFTER INSERT ON chapters BEGIN
  INSERT INTO chapters_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chapters_ad AFTER DELETE ON chapters BEGIN
  INSERT INTO chapters_fts(chapters_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS chapters_au AFTER UPDATE ON chapters BEGIN
  INSERT INTO chapters_fts(chapters_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
  INSERT INTO chapters_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;

CREATE TABLE IF NOT EXISTS import_jobs (
  id         TEXT PRIMARY KEY,
  status     TEXT DEFAULT 'pending',   -- pending | running | done | error
  total      INTEGER DEFAULT 0,
  done       INTEGER DEFAULT 0,
  ok         INTEGER DEFAULT 0,
  failed     INTEGER DEFAULT 0,
  log        TEXT DEFAULT '[]',        -- JSON 数组，逐本结果
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);
