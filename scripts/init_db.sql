-- Incubator shared Postgres — initial setup
-- Locks down the public schema so app users can only access their own schema.

-- Revoke default public access
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- Only the admin user can use public schema
GRANT ALL ON SCHEMA public TO aihub_admin;
