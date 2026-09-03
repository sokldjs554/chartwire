-- docker-compose 전용 초기화 스크립트 (postgres:16 이미지의 /docker-entrypoint-initdb.d 에서 superuser 로 1회 실행).
-- 운영/CI 에서는 같은 일을 `chartwire db bootstrap-roles` 가 멱등하게 수행한다 (spec §4.1). 비밀번호는 .env.example 과 동일한 개발용 값.
CREATE ROLE chartwire_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD 'chartwire_owner';
CREATE ROLE chartwire_app   LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD 'chartwire_app';
ALTER ROLE chartwire_app SET statement_timeout = '5s';

CREATE DATABASE chartwire OWNER chartwire_owner;
GRANT CONNECT ON DATABASE chartwire TO chartwire_app;

\connect chartwire
SET ROLE chartwire_owner;                       -- 확장 소유자도 owner (superuser 소유 객체를 남기지 않는다)
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
GRANT USAGE ON SCHEMA public TO chartwire_app;  -- 테이블별 GRANT 는 마이그레이션이 수행
