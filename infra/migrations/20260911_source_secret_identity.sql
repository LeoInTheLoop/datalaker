-- Existing demo volumes do not rerun init-steward.sql. Keep the connection
-- identity and its narrow writer in sync without granting the Agent access to
-- the credential itself.
ALTER TABLE source_secrets ADD COLUMN IF NOT EXISTS identity TEXT;

UPDATE source_secrets
   SET identity = split_part(split_part(dsn, '://', 2), ':', 1) || '@'
                  || split_part(split_part(dsn, '://', 2), '@', 2)
 WHERE identity IS NULL OR identity = '';

CREATE OR REPLACE FUNCTION datasteward_put_source_secret(
    p_source_id text, p_dsn text, p_kind text, p_approval_id text, p_by text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  v_identity text;
BEGIN
  v_identity := split_part(split_part(p_dsn, '://', 2), ':', 1) || '@'
                || split_part(split_part(p_dsn, '://', 2), '@', 2);
  UPDATE public.source_secrets
     SET dsn = p_dsn, identity = v_identity, kind = p_kind,
         approval_id = p_approval_id, registered_by = p_by,
         registered_at = extract(epoch from now())
   WHERE source_id = p_source_id;
  IF NOT FOUND THEN
    INSERT INTO public.source_secrets
      (source_id, dsn, identity, kind, approval_id, registered_by, registered_at)
    VALUES
      (p_source_id, p_dsn, v_identity, p_kind, p_approval_id, p_by,
       extract(epoch from now()));
  END IF;
END;
$$;
ALTER FUNCTION datasteward_put_source_secret(text,text,text,text,text) OWNER TO postgres;
REVOKE ALL ON FUNCTION datasteward_put_source_secret(text,text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION datasteward_put_source_secret(text,text,text,text,text) TO agent_role;
GRANT SELECT (source_id, identity, kind, approval_id) ON source_secrets TO agent_role;
