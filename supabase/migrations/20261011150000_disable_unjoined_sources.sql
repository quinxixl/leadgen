-- Enabled Telegram sources must be chats the shared service account follows: the reader gets
-- messages only from them, and the cabinet catalog, chat-add checks and status count enabled rows.
-- The old imported catalog was enabled by public-page activity but is never read, so those rows are
-- switched off. Joined chats (marked by the reader's dialog sync or a live message) stay enabled, and
-- the reader re-enables any chat it joins later. Nothing is deleted; a re-run changes no rows.
update leadgen.telegram_sources
set enabled = 0,
    check_reason = 'служебный аккаунт не подписан'
where enabled = 1
  and coalesce(selection_group, '') <> 'joined'
  and coalesce(segment, '') <> 'joined'
  and coalesce(metadata, '') !~ '"joined"[[:space:]]*:[[:space:]]*true';
