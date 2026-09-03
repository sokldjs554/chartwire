--
-- PostgreSQL database dump
--

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: ensure_segment_partition(date); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ensure_segment_partition(p_month date) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
  v_name text := format('transcript_segments_y%sm%s', to_char(p_month, 'YYYY'), to_char(p_month, 'MM'));
  v_from date := date_trunc('month', p_month);
  v_idx  text := 'ix_segments_patient_time_' || format('y%sm%s', to_char(p_month, 'YYYY'), to_char(p_month, 'MM'));
  v_auto text;
BEGIN
  IF to_regclass(v_name) IS NULL THEN
    EXECUTE format('CREATE TABLE %I PARTITION OF transcript_segments FOR VALUES FROM (%L) TO (%L)',
                   v_name, v_from, v_from + interval '1 month');
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', v_name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', v_name);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', v_name);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chartwire_app') THEN
      EXECUTE format('GRANT SELECT, INSERT, DELETE ON %I TO chartwire_app', v_name);
    END IF;
  END IF;

  -- 0007: keep the partitioned index ix_segments_patient_time complete. CREATE TABLE ... PARTITION OF
  -- auto-creates and attaches a matching index; normalize its name, or create + attach it ourselves.
  IF to_regclass('ix_segments_patient_time') IS NOT NULL THEN
    SELECT c.relname INTO v_auto
      FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_index x ON x.indexrelid = c.oid
     WHERE i.inhparent = 'ix_segments_patient_time'::regclass AND x.indrelid = v_name::regclass;
    IF v_auto IS NULL THEN
      EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (tenant_id, patient_id, created_at DESC, id DESC)', v_idx, v_name);
      EXECUTE format('ALTER INDEX ix_segments_patient_time ATTACH PARTITION %I', v_idx);
    ELSIF v_auto <> v_idx THEN
      EXECUTE format('ALTER INDEX %I RENAME TO %I', v_auto, v_idx);
    END IF;
  END IF;
  RETURN v_name;
END $$;


--
-- Name: search_segments(text, text, uuid, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.search_segments(p_query text, p_mode text, p_patient uuid DEFAULT NULL::uuid, p_limit integer DEFAULT 50) RETURNS TABLE(segment_id bigint, session_id uuid, patient_id uuid, speaker text, snippet text, segment_created_at timestamp with time zone)
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE v_tenant uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
BEGIN
  IF v_tenant IS NULL THEN RAISE EXCEPTION 'tenant context missing' USING ERRCODE = '42501'; END IF;
  IF p_mode = 'term' THEN
    RETURN QUERY SELECT s.segment_id, s.session_id, s.patient_id, s.speaker, left(s.text, 120), s.segment_created_at
      FROM segment_search s WHERE s.tenant_id = v_tenant AND s.terms @> ARRAY[p_query] AND (p_patient IS NULL OR s.patient_id = p_patient)
      ORDER BY s.segment_created_at DESC LIMIT p_limit;
  ELSE
    IF length(p_query) < 3 THEN RAISE EXCEPTION 'free-text search needs >= 3 characters' USING ERRCODE = '22023'; END IF;
    RETURN QUERY SELECT s.segment_id, s.session_id, s.patient_id, s.speaker, left(s.text, 120), s.segment_created_at
      FROM segment_search s WHERE s.tenant_id = v_tenant AND s.text ILIKE '%' || p_query || '%' AND (p_patient IS NULL OR s.patient_id = p_patient)
      ORDER BY s.segment_created_at DESC LIMIT p_limit;
  END IF;
END $$;


--
-- Name: trg_audit_immutable(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_audit_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN RAISE EXCEPTION 'audit_events is append-only'; END $$;


--
-- Name: trg_dek_no_resurrect(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_dek_no_resurrect() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF OLD.dek_destroyed_at IS NOT NULL AND NEW.dek_wrapped IS NOT NULL THEN RAISE EXCEPTION 'DEK destroyed'; END IF;
  RETURN NEW;
END $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: audio_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audio_chunks (
    session_id uuid NOT NULL,
    seq bigint NOT NULL,
    tenant_id uuid NOT NULL,
    byte_len integer NOT NULL,
    sha256 bytea NOT NULL,
    storage_key text NOT NULL,
    offset_ms integer NOT NULL,
    flags smallint DEFAULT 0 NOT NULL,
    received_at timestamp with time zone NOT NULL
);

ALTER TABLE ONLY public.audio_chunks FORCE ROW LEVEL SECURITY;


--
-- Name: audit_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_events (
    id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    actor_id uuid,
    actor_role text,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id text,
    request_id text,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE ONLY public.audit_events FORCE ROW LEVEL SECURITY;


--
-- Name: audit_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.audit_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.audit_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: consents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.consents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    scopes text[] NOT NULL,
    version integer NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    granted_by uuid,
    channel text DEFAULT 'console'::text NOT NULL,
    policy_hash bytea,
    revoked_at timestamp with time zone,
    revoked_by uuid,
    revoked_reason text,
    CONSTRAINT consents_scopes_check CHECK ((scopes <@ ARRAY['recording'::text, 'transcription'::text, 'ai_drafting'::text, 'search_index'::text]))
);

ALTER TABLE ONLY public.consents FORCE ROW LEVEL SECURITY;


--
-- Name: dead_letters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dead_letters (
    id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    outbox_event_id bigint NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    attempts integer NOT NULL,
    last_error text,
    died_at timestamp with time zone DEFAULT now() NOT NULL,
    replayed_at timestamp with time zone
);

ALTER TABLE ONLY public.dead_letters FORCE ROW LEVEL SECURITY;


--
-- Name: dead_letters_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.dead_letters ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.dead_letters_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: note_assessments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.note_assessments (
    note_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    text_enc bytea NOT NULL,
    author_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.note_assessments FORCE ROW LEVEL SECURITY;


--
-- Name: note_statements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.note_statements (
    id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    note_id uuid NOT NULL,
    section character(1) NOT NULL,
    ordinal integer NOT NULL,
    text_enc bytea NOT NULL,
    evidence jsonb NOT NULL,
    verdict text NOT NULL,
    verdict_reason text,
    method text,
    clinician_decision text,
    edited_text_enc bytea,
    CONSTRAINT note_statements_clinician_decision_check CHECK ((clinician_decision = ANY (ARRAY['accept'::text, 'edit'::text, 'reject'::text]))),
    CONSTRAINT note_statements_section_check CHECK ((section = ANY (ARRAY['S'::bpchar, 'O'::bpchar, 'P'::bpchar]))),
    CONSTRAINT note_statements_verdict_check CHECK ((verdict = ANY (ARRAY['supported'::text, 'unsupported'::text])))
);

ALTER TABLE ONLY public.note_statements FORCE ROW LEVEL SECURITY;


--
-- Name: note_statements_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.note_statements ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.note_statements_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notes (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    version integer NOT NULL,
    status text NOT NULL,
    provider text NOT NULL,
    model text,
    prompt_hash bytea,
    grounding_coverage numeric(5,4),
    statement_count integer DEFAULT 0 NOT NULL,
    unsupported_count integer DEFAULT 0 NOT NULL,
    abstain_reason text,
    raw_draft_enc bytea,
    signed_content_enc bytea,
    legal_hold text,
    retention_until timestamp with time zone,
    signed_by uuid,
    signed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT notes_legal_hold_check CHECK ((legal_hold = 'medical_record'::text)),
    CONSTRAINT notes_status_check CHECK ((status = ANY (ARRAY['drafting'::text, 'needs_review'::text, 'verified'::text, 'abstained'::text, 'signed'::text, 'rejected'::text])))
);

ALTER TABLE ONLY public.notes FORCE ROW LEVEL SECURITY;


--
-- Name: outbox_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.outbox_events (
    id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    idempotency_key text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    locked_by text,
    locked_at timestamp with time zone,
    lease_until timestamp with time zone,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    done_at timestamp with time zone,
    CONSTRAINT outbox_events_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'in_flight'::text, 'done'::text, 'dead'::text])))
);

ALTER TABLE ONLY public.outbox_events FORCE ROW LEVEL SECURITY;


--
-- Name: outbox_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.outbox_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.outbox_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: patients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.patients (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    pseudonym text NOT NULL,
    name_enc bytea,
    name_hmac bytea,
    birth_year integer,
    sex character(1),
    phone_enc bytea,
    dek_wrapped bytea,
    dek_fingerprint bytea,
    dek_destroyed_at timestamp with time zone,
    consent_state text DEFAULT 'none'::text NOT NULL,
    is_synthetic boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    purged_at timestamp with time zone,
    CONSTRAINT patients_consent_state_check CHECK ((consent_state = ANY (ARRAY['none'::text, 'granted'::text, 'revoked'::text, 'purged'::text])))
);

ALTER TABLE ONLY public.patients FORCE ROW LEVEL SECURITY;


--
-- Name: processed_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.processed_events (
    handler text NOT NULL,
    event_id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    processed_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.processed_events FORCE ROW LEVEL SECURITY;


--
-- Name: purge_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.purge_jobs (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    subject_type text NOT NULL,
    subject_id uuid NOT NULL,
    reason text NOT NULL,
    requested_by uuid,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    state text DEFAULT 'queued'::text NOT NULL,
    steps jsonb DEFAULT '[]'::jsonb NOT NULL,
    counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    dek_fingerprints jsonb DEFAULT '[]'::jsonb NOT NULL,
    sample_ciphertext bytea,
    receipt_hash bytea,
    completed_at timestamp with time zone,
    verified_at timestamp with time zone,
    verify_result jsonb,
    CONSTRAINT purge_jobs_reason_check CHECK ((reason = ANY (ARRAY['consent_revoked'::text, 'admin'::text, 'retention'::text]))),
    CONSTRAINT purge_jobs_state_check CHECK ((state = ANY (ARRAY['queued'::text, 'running'::text, 'completed'::text, 'verified'::text, 'failed'::text]))),
    CONSTRAINT purge_jobs_subject_type_check CHECK ((subject_type = ANY (ARRAY['session'::text, 'patient'::text])))
);

ALTER TABLE ONLY public.purge_jobs FORCE ROW LEVEL SECURITY;


--
-- Name: risk_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.risk_events (
    id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    segment_id bigint NOT NULL,
    segment_created_at timestamp with time zone NOT NULL,
    segment_seq integer NOT NULL,
    category text NOT NULL,
    severity smallint NOT NULL,
    phrase text NOT NULL,
    span_start integer NOT NULL,
    span_end integer NOT NULL,
    scope jsonb DEFAULT '{}'::jsonb NOT NULL,
    detector_version text NOT NULL,
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    sla_deadline_at timestamp with time zone,
    acknowledged_at timestamp with time zone,
    acknowledged_by uuid,
    escalated_at timestamp with time zone,
    escalation_level smallint DEFAULT 0 NOT NULL,
    CONSTRAINT risk_events_category_check CHECK ((category = ANY (ARRAY['suicidal_ideation'::text, 'self_harm'::text, 'harm_to_others'::text, 'substance_acute'::text]))),
    CONSTRAINT risk_events_severity_check CHECK (((severity >= 1) AND (severity <= 3)))
);

ALTER TABLE ONLY public.risk_events FORCE ROW LEVEL SECURITY;


--
-- Name: risk_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.risk_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.risk_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: segment_search; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.segment_search (
    segment_id bigint NOT NULL,
    segment_created_at timestamp with time zone NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    speaker text NOT NULL,
    text text NOT NULL,
    terms text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sessions (
    id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    clinician_id uuid NOT NULL,
    state text DEFAULT 'created'::text NOT NULL,
    script_ref text,
    codec text DEFAULT 'pcm16le'::text NOT NULL,
    sample_rate integer DEFAULT 16000 NOT NULL,
    chunk_ms integer DEFAULT 200 NOT NULL,
    stt_provider text DEFAULT 'simulator'::text NOT NULL,
    scopes_snapshot text[] DEFAULT '{}'::text[] NOT NULL,
    dek_wrapped bytea,
    dek_fingerprint bytea,
    dek_destroyed_at timestamp with time zone,
    ack_seq bigint DEFAULT 0 NOT NULL,
    final_seq bigint,
    epoch integer DEFAULT 0 NOT NULL,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    transcribed_at timestamp with time zone,
    signed_at timestamp with time zone,
    purged_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT sessions_state_check CHECK ((state = ANY (ARRAY['created'::text, 'recording'::text, 'paused'::text, 'ended'::text, 'transcribed'::text, 'drafted'::text, 'signed'::text, 'purging'::text, 'purged'::text])))
);

ALTER TABLE ONLY public.sessions FORCE ROW LEVEL SECURITY;


--
-- Name: stt_offsets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stt_offsets (
    session_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    last_chunk_seq bigint DEFAULT 0 NOT NULL,
    last_segment_seq integer DEFAULT '-1'::integer NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.stt_offsets FORCE ROW LEVEL SECURITY;


--
-- Name: tenants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    kek_ref text NOT NULL,
    record_key_wrapped bytea NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tenants_status_check CHECK ((status = ANY (ARRAY['active'::text, 'suspended'::text])))
);


--
-- Name: transcript_segments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.transcript_segments (
    id bigint NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    seq integer NOT NULL,
    speaker text NOT NULL,
    t_start_ms integer NOT NULL,
    t_end_ms integer NOT NULL,
    text_enc bytea NOT NULL,
    text_len integer NOT NULL,
    confidence real,
    provider text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT transcript_segments_speaker_check CHECK ((speaker = ANY (ARRAY['clinician'::text, 'patient'::text, 'unknown'::text])))
)
PARTITION BY RANGE (created_at);

ALTER TABLE ONLY public.transcript_segments FORCE ROW LEVEL SECURITY;


--
-- Name: transcript_segments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.transcript_segments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: transcript_segments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.transcript_segments_id_seq OWNED BY public.transcript_segments.id;


--
-- Name: transcript_segments_default; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.transcript_segments_default (
    id bigint DEFAULT nextval('public.transcript_segments_id_seq'::regclass) NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    seq integer NOT NULL,
    speaker text NOT NULL,
    t_start_ms integer NOT NULL,
    t_end_ms integer NOT NULL,
    text_enc bytea NOT NULL,
    text_len integer NOT NULL,
    confidence real,
    provider text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT transcript_segments_speaker_check CHECK ((speaker = ANY (ARRAY['clinician'::text, 'patient'::text, 'unknown'::text])))
);

ALTER TABLE ONLY public.transcript_segments_default FORCE ROW LEVEL SECURITY;


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    role text NOT NULL,
    email_hmac bytea NOT NULL,
    email_enc bytea NOT NULL,
    display_name text NOT NULL,
    password_hash text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT users_role_check CHECK ((role = ANY (ARRAY['clinician'::text, 'staff'::text, 'admin'::text, 'auditor'::text, 'recorder'::text])))
);

ALTER TABLE ONLY public.users FORCE ROW LEVEL SECURITY;


--
-- Name: transcript_segments_default; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transcript_segments ATTACH PARTITION public.transcript_segments_default DEFAULT;


--
-- Name: transcript_segments id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transcript_segments ALTER COLUMN id SET DEFAULT nextval('public.transcript_segments_id_seq'::regclass);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: audio_chunks audio_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audio_chunks
    ADD CONSTRAINT audio_chunks_pkey PRIMARY KEY (session_id, seq);


--
-- Name: audit_events audit_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT audit_events_pkey PRIMARY KEY (id);


--
-- Name: consents consents_patient_id_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consents
    ADD CONSTRAINT consents_patient_id_version_key UNIQUE (patient_id, version);


--
-- Name: consents consents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consents
    ADD CONSTRAINT consents_pkey PRIMARY KEY (id);


--
-- Name: dead_letters dead_letters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dead_letters
    ADD CONSTRAINT dead_letters_pkey PRIMARY KEY (id);


--
-- Name: note_assessments note_assessments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_assessments
    ADD CONSTRAINT note_assessments_pkey PRIMARY KEY (note_id);


--
-- Name: note_statements note_statements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_statements
    ADD CONSTRAINT note_statements_pkey PRIMARY KEY (id);


--
-- Name: notes notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notes
    ADD CONSTRAINT notes_pkey PRIMARY KEY (id);


--
-- Name: notes notes_session_id_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notes
    ADD CONSTRAINT notes_session_id_version_key UNIQUE (session_id, version);


--
-- Name: outbox_events outbox_events_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbox_events
    ADD CONSTRAINT outbox_events_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: outbox_events outbox_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.outbox_events
    ADD CONSTRAINT outbox_events_pkey PRIMARY KEY (id);


--
-- Name: patients patients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT patients_pkey PRIMARY KEY (id);


--
-- Name: patients patients_tenant_id_pseudonym_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT patients_tenant_id_pseudonym_key UNIQUE (tenant_id, pseudonym);


--
-- Name: processed_events processed_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processed_events
    ADD CONSTRAINT processed_events_pkey PRIMARY KEY (handler, event_id);


--
-- Name: purge_jobs purge_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purge_jobs
    ADD CONSTRAINT purge_jobs_pkey PRIMARY KEY (id);


--
-- Name: risk_events risk_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.risk_events
    ADD CONSTRAINT risk_events_pkey PRIMARY KEY (id);


--
-- Name: segment_search segment_search_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.segment_search
    ADD CONSTRAINT segment_search_pkey PRIMARY KEY (segment_id);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);


--
-- Name: stt_offsets stt_offsets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stt_offsets
    ADD CONSTRAINT stt_offsets_pkey PRIMARY KEY (session_id);


--
-- Name: tenants tenants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);


--
-- Name: tenants tenants_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_slug_key UNIQUE (slug);


--
-- Name: transcript_segments transcript_segments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transcript_segments
    ADD CONSTRAINT transcript_segments_pkey PRIMARY KEY (created_at, id);


--
-- Name: transcript_segments_default transcript_segments_default_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transcript_segments_default
    ADD CONSTRAINT transcript_segments_default_pkey PRIMARY KEY (created_at, id);


--
-- Name: transcript_segments transcript_segments_session_id_seq_created_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transcript_segments
    ADD CONSTRAINT transcript_segments_session_id_seq_created_at_key UNIQUE (session_id, seq, created_at);


--
-- Name: transcript_segments_default transcript_segments_default_session_id_seq_created_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transcript_segments_default
    ADD CONSTRAINT transcript_segments_default_session_id_seq_created_at_key UNIQUE (session_id, seq, created_at);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_tenant_id_email_hmac_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_tenant_id_email_hmac_key UNIQUE (tenant_id, email_hmac);


--
-- Name: ix_audit_tenant_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_tenant_time ON public.audit_events USING btree (tenant_id, at DESC);


--
-- Name: ix_consents_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_consents_active ON public.consents USING btree (tenant_id, patient_id) WHERE (revoked_at IS NULL);


--
-- Name: ix_note_statements; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_statements ON public.note_statements USING btree (note_id, section, ordinal);


--
-- Name: ix_outbox_aggregate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outbox_aggregate ON public.outbox_events USING btree (aggregate_id);


--
-- Name: ix_outbox_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_outbox_pending ON public.outbox_events USING btree (next_attempt_at, id) WHERE (status = 'pending'::text);


--
-- Name: ix_patients_name_hmac; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patients_name_hmac ON public.patients USING btree (tenant_id, name_hmac);


--
-- Name: ix_risk_open_sla; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_risk_open_sla ON public.risk_events USING btree (tenant_id, sla_deadline_at) WHERE (acknowledged_at IS NULL);


--
-- Name: ix_risk_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_risk_session ON public.risk_events USING btree (session_id, detected_at);


--
-- Name: ix_risk_tenant_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_risk_tenant_time ON public.risk_events USING btree (tenant_id, detected_at DESC);


--
-- Name: ix_search_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_search_patient ON public.segment_search USING btree (tenant_id, patient_id, segment_created_at DESC);


--
-- Name: ix_search_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_search_session ON public.segment_search USING btree (tenant_id, session_id);


--
-- Name: ix_search_terms; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_search_terms ON public.segment_search USING gin (terms);


--
-- Name: ix_search_text_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_search_text_trgm ON public.segment_search USING gin (text public.gin_trgm_ops);


--
-- Name: ix_segments_patient_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_segments_patient_time ON ONLY public.transcript_segments USING btree (tenant_id, patient_id, created_at DESC, id DESC);


--
-- Name: ix_segments_patient_time_default; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_segments_patient_time_default ON public.transcript_segments_default USING btree (tenant_id, patient_id, created_at DESC, id DESC);


--
-- Name: ix_sessions_clinician; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sessions_clinician ON public.sessions USING btree (tenant_id, clinician_id, created_at DESC);


--
-- Name: ix_sessions_live; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sessions_live ON public.sessions USING btree (tenant_id) WHERE (state = ANY (ARRAY['recording'::text, 'paused'::text]));


--
-- Name: ix_sessions_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sessions_patient ON public.sessions USING btree (tenant_id, patient_id, created_at DESC);


--
-- Name: ix_segments_patient_time_default; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_segments_patient_time ATTACH PARTITION public.ix_segments_patient_time_default;


--
-- Name: transcript_segments_default_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.transcript_segments_pkey ATTACH PARTITION public.transcript_segments_default_pkey;


--
-- Name: transcript_segments_default_session_id_seq_created_at_key; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.transcript_segments_session_id_seq_created_at_key ATTACH PARTITION public.transcript_segments_default_session_id_seq_created_at_key;


--
-- Name: audit_events audit_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER audit_no_update BEFORE DELETE OR UPDATE ON public.audit_events FOR EACH ROW EXECUTE FUNCTION public.trg_audit_immutable();


--
-- Name: patients patients_dek_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER patients_dek_guard BEFORE UPDATE ON public.patients FOR EACH ROW EXECUTE FUNCTION public.trg_dek_no_resurrect();


--
-- Name: sessions sessions_dek_guard; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER sessions_dek_guard BEFORE UPDATE ON public.sessions FOR EACH ROW EXECUTE FUNCTION public.trg_dek_no_resurrect();


--
-- Name: audio_chunks audio_chunks_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audio_chunks
    ADD CONSTRAINT audio_chunks_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id);


--
-- Name: consents consents_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.consents
    ADD CONSTRAINT consents_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id);


--
-- Name: note_assessments note_assessments_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_assessments
    ADD CONSTRAINT note_assessments_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.notes(id);


--
-- Name: note_statements note_statements_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_statements
    ADD CONSTRAINT note_statements_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.notes(id);


--
-- Name: notes notes_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notes
    ADD CONSTRAINT notes_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id);


--
-- Name: patients patients_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patients
    ADD CONSTRAINT patients_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: risk_events risk_events_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.risk_events
    ADD CONSTRAINT risk_events_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id);


--
-- Name: sessions sessions_clinician_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_clinician_id_fkey FOREIGN KEY (clinician_id) REFERENCES public.users(id);


--
-- Name: sessions sessions_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patients(id);


--
-- Name: sessions sessions_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: stt_offsets stt_offsets_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stt_offsets
    ADD CONSTRAINT stt_offsets_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id);


--
-- Name: transcript_segments transcript_segments_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.transcript_segments
    ADD CONSTRAINT transcript_segments_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id);


--
-- Name: users users_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);


--
-- Name: audio_chunks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.audio_chunks ENABLE ROW LEVEL SECURITY;

--
-- Name: audit_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;

--
-- Name: audit_events audit_read_gate; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY audit_read_gate ON public.audit_events AS RESTRICTIVE FOR SELECT USING ((current_setting('app.role'::text, true) = ANY (ARRAY['auditor'::text, 'admin'::text, 'service'::text])));


--
-- Name: consents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.consents ENABLE ROW LEVEL SECURITY;

--
-- Name: dead_letters; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.dead_letters ENABLE ROW LEVEL SECURITY;

--
-- Name: note_assessments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.note_assessments ENABLE ROW LEVEL SECURITY;

--
-- Name: note_statements; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.note_statements ENABLE ROW LEVEL SECURITY;

--
-- Name: notes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notes ENABLE ROW LEVEL SECURITY;

--
-- Name: outbox_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.outbox_events ENABLE ROW LEVEL SECURITY;

--
-- Name: patients; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.patients ENABLE ROW LEVEL SECURITY;

--
-- Name: processed_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.processed_events ENABLE ROW LEVEL SECURITY;

--
-- Name: purge_jobs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.purge_jobs ENABLE ROW LEVEL SECURITY;

--
-- Name: risk_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.risk_events ENABLE ROW LEVEL SECURITY;

--
-- Name: note_assessments role_gate; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY role_gate ON public.note_assessments AS RESTRICTIVE USING ((current_setting('app.role'::text, true) = ANY (ARRAY['clinician'::text, 'service'::text, 'auditor'::text])));


--
-- Name: note_statements role_gate; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY role_gate ON public.note_statements AS RESTRICTIVE USING ((current_setting('app.role'::text, true) = ANY (ARRAY['clinician'::text, 'service'::text, 'auditor'::text])));


--
-- Name: notes role_gate; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY role_gate ON public.notes AS RESTRICTIVE USING ((current_setting('app.role'::text, true) = ANY (ARRAY['clinician'::text, 'service'::text, 'auditor'::text])));


--
-- Name: segment_search; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.segment_search ENABLE ROW LEVEL SECURITY;

--
-- Name: sessions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY;

--
-- Name: stt_offsets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.stt_offsets ENABLE ROW LEVEL SECURITY;

--
-- Name: audio_chunks tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.audio_chunks USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: audit_events tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.audit_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: consents tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.consents USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: dead_letters tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.dead_letters USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: note_assessments tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.note_assessments USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: note_statements tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.note_statements USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: notes tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.notes USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: outbox_events tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.outbox_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: patients tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.patients USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: processed_events tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.processed_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: purge_jobs tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.purge_jobs USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: risk_events tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.risk_events USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: segment_search tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.segment_search USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: sessions tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.sessions USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: stt_offsets tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.stt_offsets USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: transcript_segments tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.transcript_segments USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: transcript_segments_default tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.transcript_segments_default USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: users tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.users USING ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)) WITH CHECK ((tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid));


--
-- Name: transcript_segments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.transcript_segments ENABLE ROW LEVEL SECURITY;

--
-- Name: transcript_segments_default; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.transcript_segments_default ENABLE ROW LEVEL SECURITY;

--
-- Name: users; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;

--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA public TO chartwire_app;


--
-- Name: FUNCTION ensure_segment_partition(p_month date); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.ensure_segment_partition(p_month date) FROM PUBLIC;
GRANT ALL ON FUNCTION public.ensure_segment_partition(p_month date) TO chartwire_app;


--
-- Name: FUNCTION search_segments(p_query text, p_mode text, p_patient uuid, p_limit integer); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.search_segments(p_query text, p_mode text, p_patient uuid, p_limit integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.search_segments(p_query text, p_mode text, p_patient uuid, p_limit integer) TO chartwire_app;


--
-- Name: TABLE audio_chunks; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audio_chunks TO chartwire_app;


--
-- Name: TABLE audit_events; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.audit_events TO chartwire_app;


--
-- Name: SEQUENCE audit_events_id_seq; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE public.audit_events_id_seq TO chartwire_app;


--
-- Name: TABLE consents; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.consents TO chartwire_app;


--
-- Name: TABLE dead_letters; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dead_letters TO chartwire_app;


--
-- Name: TABLE note_assessments; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.note_assessments TO chartwire_app;


--
-- Name: TABLE note_statements; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.note_statements TO chartwire_app;


--
-- Name: TABLE notes; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notes TO chartwire_app;


--
-- Name: TABLE outbox_events; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.outbox_events TO chartwire_app;


--
-- Name: TABLE patients; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.patients TO chartwire_app;


--
-- Name: TABLE processed_events; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.processed_events TO chartwire_app;


--
-- Name: TABLE purge_jobs; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.purge_jobs TO chartwire_app;


--
-- Name: TABLE risk_events; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.risk_events TO chartwire_app;


--
-- Name: TABLE segment_search; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.segment_search TO chartwire_app;


--
-- Name: TABLE sessions; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sessions TO chartwire_app;


--
-- Name: TABLE stt_offsets; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.stt_offsets TO chartwire_app;


--
-- Name: TABLE tenants; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT ON TABLE public.tenants TO chartwire_app;


--
-- Name: TABLE transcript_segments; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE ON TABLE public.transcript_segments TO chartwire_app;


--
-- Name: SEQUENCE transcript_segments_id_seq; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,USAGE ON SEQUENCE public.transcript_segments_id_seq TO chartwire_app;


--
-- Name: TABLE transcript_segments_default; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE ON TABLE public.transcript_segments_default TO chartwire_app;


--
-- Name: TABLE users; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.users TO chartwire_app;


--
-- PostgreSQL database dump complete
--
