alter table leadgen.telegram_connection_auth
    drop constraint if exists telegram_connection_auth_stage_check;

alter table leadgen.telegram_connection_auth
    add constraint telegram_connection_auth_stage_check
    check (stage in ('code','password','qr'));
