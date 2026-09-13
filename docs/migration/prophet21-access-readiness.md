# Prophet 21 Read-Only Access & Schema Readiness Checklist

> **AUDIT PURPOSE**: This document outlines the technical and security readiness requirements for connecting Bop ERP as a read-only destination to an upstream Prophet 21 (P21) system.
> **PERMANENT SAFETY MANDATE**: Bop ERP operates strictly as a read-only subscriber. No data will ever be written, updated, or deleted on the Prophet 21 instance by Bop ERP.

---

## 1. System Overview & Architecture

- **Migration Direction**: `Prophet 21 (Source ERP) -> Bop ERP (Destination ERP)` (STRICTLY ONE-WAY)
- **Execution Mode**: Offline extracts, dedicated reporting replica, or isolated database snapshot restore.
- **Production Safety Policy**: Zero live production writes, zero stored procedure executions, zero background data mutation jobs.

---

## 2. Client IT Information Request

Please complete and return the following technical parameters (do **not** include passwords in this form):

| Parameter | Description | Client IT Response / Value |
| :--- | :--- | :--- |
| **Prophet 21 Build/Version** | e.g., 2021.2, 2023.1 Cloud/On-Prem | |
| **Underlying Database Engine** | Microsoft SQL Server version / edition | |
| **Preferred Access Mode** | e.g., Restored Snapshot, Dedicated Replica, ODBC | |
| **Database Server Name / Host** | Hostname or internal IP (non-public) | |
| **Database Name** | Name of the P21 production/reporting database (required) | |
| **Network Security Requirements** | VPN required? IP Allowlisting required? | |
| **Database Timezone** | Server timezone (e.g., UTC, America/Bogota, EST) | |
| **Database Collation** | Server / Database default collation | |
| **Company Scope Mode** | Must confirm one of: `SINGLE_COMPANY_DATABASE`, `SEPARATE_DATABASE_PER_COMPANY`, `EXPLICIT_COLUMN`, `RELATIONAL_MAPPING`, `OTHER_VERIFIED` | |
| **Source Company Identifier(s)** | e.g., `company_id`, branch IDs, or `N/A` if single-company | |
| **Target Bop Company** | Destination Company in Bop ERP (default: `Industrial DP`) | |
| **Location / Branch Hierarchy** | How branches/warehouses map to company entities | |

### 2.1 Mandatory Company Scope Confirmation (Fail-Closed Policy)

To prevent cross-tenant data contamination or partial data migration, Bop ERP requires unambiguous proof of company segregation before a data source can be classified as `READY`. Client IT must declare and confirm the applicable mode:

1. **`SINGLE_COMPANY_DATABASE`**: The database contains only records for a single operating entity. No tenant segregation column is present or required.
2. **`SEPARATE_DATABASE_PER_COMPANY`**: Multi-company setup where each entity resides in its own discrete database catalog.
3. **`EXPLICIT_COLUMN`**: Multi-tenant database where tables contain a discriminator column (e.g., `company_id`, `company_no`).
4. **`RELATIONAL_MAPPING`**: Company tenancy is inferred via relational association (e.g., branch / location mapping table).
5. **`OTHER_VERIFIED`**: Alternative documented and audit-verified segregation scheme.

> **FAIL-CLOSED POLICY**: If the company scope mode is `UNKNOWN` or unconfirmed, readiness status will remain strictly **`BLOCKED`**.

---

## 3. Preferred Access Hierarchy

To ensure zero production risk, Bop ERP requests access according to the following order of preference:

1. **Restored Database Snapshot (Highest Safety & Recommended)**
   - A standard SQL Server `.bak` backup restored into an isolated secondary staging/reporting environment.
   - Eliminates 100% of operational risk to live ERP transactions.
2. **Dedicated Read-Only Reporting Replica (High Safety)**
   - SQL Server AlwaysOn availability group read-only replica or log-shipped copy.
   - Isolated CPU/Memory from transactional users.
3. **Dedicated Read-Only SQL Account on Live Instance (Conditional)**
   - Account strictly granted `SELECT` only on required master tables/views.
   - Enforced command timeouts (max 60s) and bounded keyset batching.
4. **Structured CSV / Flat-File Dumps (Contingency)**
   - Direct tab/comma-delimited extracts provided by client IT per agreed schema catalog.

---

## 4. Minimum SQL Server Privilege Contract

If providing direct SQL Server / ODBC credentials, the database account **must conform to this least-privilege contract**:

### Required Grants:
```sql
-- Explicitly grant CONNECT and SELECT only on the Prophet 21 database
USE [p21_database];
CREATE LOGIN [bop_migration_ro] WITH PASSWORD = '<StrongSecretKey>', CHECK_POLICY = ON;
CREATE USER [bop_migration_ro] FOR LOGIN [bop_migration_ro];

-- Grant SELECT only on relevant tables/views
GRANT CONNECT TO [bop_migration_ro];
GRANT SELECT ON SCHEMA::dbo TO [bop_migration_ro];
GRANT VIEW DEFINITION TO [bop_migration_ro];
```

### Strictly Forbidden Permissions:
The account **must NOT possess** any of the following roles or permissions:
- `db_owner`, `db_datawriter`, `sysadmin`, `serveradmin`
- `INSERT`, `UPDATE`, `DELETE`, `MERGE`
- `EXECUTE` on any stored procedures (`sp_*`)
- `ALTER`, `DROP`, `CREATE`, `TRUNCATE`
- `CONTROL`

---

## 5. Metadata & Schema Intake Extraction Script

Before mapping master data, Bop ERP requires a machine-readable schema snapshot of tables, views, and columns. Client IT can execute this harmless read-only query and return the resulting JSON output:

```sql
SELECT
    t.TABLE_SCHEMA AS schema_name,
    t.TABLE_NAME AS name,
    c.COLUMN_NAME AS column_name,
    c.DATA_TYPE AS data_type,
    c.IS_NULLABLE AS is_nullable,
    c.CHARACTER_MAXIMUM_LENGTH AS character_maximum_length,
    c.NUMERIC_PRECISION AS numeric_precision,
    c.NUMERIC_SCALE AS numeric_scale,
    c.COLLATION_NAME AS collation_name
FROM INFORMATION_SCHEMA.TABLES t
INNER JOIN INFORMATION_SCHEMA.COLUMNS c
    ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
WHERE t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')
ORDER BY t.TABLE_NAME, c.ORDINAL_POSITION;
```

---

## 6. Secure Credential Exchange Policy

- **NEVER** transmit passwords via email, tickets, or text chat.
- **NEVER** commit credentials to source code or configuration repositories.
- Credentials must be exchanged exclusively via an approved encrypted channel (e.g., 1Password, Bitwarden, HashiCorp Vault, or encrypted one-time secret links).
