-- ============================================================================
-- PROPHET 21 READ-ONLY SCHEMA METADATA EXTRACTION SCRIPT
-- PURPOSE: Extract structural catalog metadata for Bop ERP migration readiness
-- SAFETY LEVEL: 100% READ-ONLY (SELECT queries only against system catalogs)
-- BUSINESS DATA: ZERO records or business columns extracted. Metadata only.
-- PRIVILEGES REQUIRED: Standard VIEW DEFINITION or SELECT on system views.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- QUERY 1: Database & Server Environment Metadata
-- ----------------------------------------------------------------------------
SELECT
    CAST(SERVERPROPERTY('MachineName') AS VARCHAR(128)) AS server_machine_name,
    CAST(SERVERPROPERTY('ServerName') AS VARCHAR(128)) AS sql_instance_name,
    CAST(SERVERPROPERTY('Edition') AS VARCHAR(128)) AS sql_server_edition,
    CAST(SERVERPROPERTY('ProductVersion') AS VARCHAR(64)) AS sql_server_version,
    CAST(SERVERPROPERTY('ProductLevel') AS VARCHAR(32)) AS sql_service_pack_level,
    CAST(SERVERPROPERTY('Collation') AS VARCHAR(128)) AS server_default_collation,
    DB_NAME() AS source_database_name,
    DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS database_collation,
    CONVERT(VARCHAR(33), SYSDATETIMEOFFSET(), 127) AS capture_timestamp_iso8601;

-- ----------------------------------------------------------------------------
-- QUERY 2: Schemas, Tables, Views & Approximate Row Counts
-- ----------------------------------------------------------------------------
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    t.type_desc AS object_type,
    ISNULL(p.row_count, 0) AS approximate_row_count
FROM sys.objects t
INNER JOIN sys.schemas s
    ON t.schema_id = s.schema_id
OUTER APPLY (
    SELECT SUM(parts.rows) AS row_count
    FROM sys.partitions parts
    WHERE parts.object_id = t.object_id
      AND parts.index_id IN (0, 1)
) p
WHERE t.type IN ('U', 'V')
  AND s.name NOT IN ('sys', 'information_schema')
ORDER BY s.name, t.name;

-- ----------------------------------------------------------------------------
-- QUERY 3: Column Specifications, Data Types & Nullability
-- ----------------------------------------------------------------------------
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    c.name AS column_name,
    c.column_id AS ordinal_position,
    ty.name AS data_type,
    c.max_length,
    c.precision,
    c.scale,
    c.is_nullable,
    c.collation_name,
    c.is_identity,
    c.is_computed
FROM sys.columns c
INNER JOIN sys.objects t
    ON c.object_id = t.object_id
INNER JOIN sys.schemas s
    ON t.schema_id = s.schema_id
INNER JOIN sys.types ty
    ON c.user_type_id = ty.user_type_id
WHERE t.type IN ('U', 'V')
  AND s.name NOT IN ('sys', 'information_schema')
ORDER BY s.name, t.name, c.column_id;

-- ----------------------------------------------------------------------------
-- QUERY 4: Primary Keys & Unique Constraints
-- ----------------------------------------------------------------------------
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    i.name AS index_name,
    i.is_primary_key,
    i.is_unique,
    c.name AS column_name,
    ic.key_ordinal AS column_ordinal
FROM sys.indexes i
INNER JOIN sys.objects t
    ON i.object_id = t.object_id
INNER JOIN sys.schemas s
    ON t.schema_id = s.schema_id
INNER JOIN sys.index_columns ic
    ON i.object_id = ic.object_id
   AND i.index_id = ic.index_id
INNER JOIN sys.columns c
    ON ic.object_id = c.object_id
   AND ic.column_id = c.column_id
WHERE t.type = 'U'
  AND (i.is_primary_key = 1 OR i.is_unique = 1)
  AND s.name NOT IN ('sys', 'information_schema')
ORDER BY s.name, t.name, i.name, ic.key_ordinal;

-- ----------------------------------------------------------------------------
-- QUERY 5: Foreign Key Relationships
-- ----------------------------------------------------------------------------
SELECT
    s_parent.name AS schema_name,
    t_parent.name AS table_name,
    fk.name AS foreign_key_name,
    c_parent.name AS column_name,
    s_ref.name AS referenced_schema_name,
    t_ref.name AS referenced_table_name,
    c_ref.name AS referenced_column_name
FROM sys.foreign_keys fk
INNER JOIN sys.foreign_key_columns fkc
    ON fk.object_id = fkc.constraint_object_id
INNER JOIN sys.objects t_parent
    ON fkc.parent_object_id = t_parent.object_id
INNER JOIN sys.schemas s_parent
    ON t_parent.schema_id = s_parent.schema_id
INNER JOIN sys.columns c_parent
    ON fkc.parent_object_id = c_parent.object_id
   AND fkc.parent_column_id = c_parent.column_id
INNER JOIN sys.objects t_ref
    ON fkc.referenced_object_id = t_ref.object_id
INNER JOIN sys.schemas s_ref
    ON t_ref.schema_id = s_ref.schema_id
INNER JOIN sys.columns c_ref
    ON fkc.referenced_object_id = c_ref.object_id
   AND fkc.referenced_column_id = c_ref.column_id
ORDER BY s_parent.name, t_parent.name, fk.name, fkc.constraint_column_id;

-- ----------------------------------------------------------------------------
-- QUERY 6: Change-Tracking, Rowversion & Timestamp Candidates
-- ----------------------------------------------------------------------------
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    c.name AS column_name,
    ty.name AS data_type,
    CASE
        WHEN ty.name IN ('timestamp', 'rowversion') THEN 'ROWVERSION_SYSTEM'
        WHEN LOWER(c.name) LIKE '%last_modified%' THEN 'AUDIT_LAST_MODIFIED'
        WHEN LOWER(c.name) LIKE '%date_modified%' THEN 'AUDIT_DATE_MODIFIED'
        WHEN LOWER(c.name) LIKE '%updated%' THEN 'AUDIT_UPDATED'
        ELSE 'OTHER_CHANGE_CANDIDATE'
    END AS candidate_category
FROM sys.columns c
INNER JOIN sys.objects t
    ON c.object_id = t.object_id
INNER JOIN sys.schemas s
    ON t.schema_id = s.schema_id
INNER JOIN sys.types ty
    ON c.user_type_id = ty.user_type_id
WHERE t.type IN ('U', 'V')
  AND s.name NOT IN ('sys', 'information_schema')
  AND (
      ty.name IN ('timestamp', 'rowversion')
      OR LOWER(c.name) LIKE '%modified%'
      OR LOWER(c.name) LIKE '%rowversion%'
      OR LOWER(c.name) LIKE '%change%'
  )
ORDER BY s.name, t.name, c.column_id;
