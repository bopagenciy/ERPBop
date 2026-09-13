# Prophet 21 (P21) Client IT Intake & Schema Capture Guide

> **CONFIDENTIAL & SECURE TECHNICAL ONBOARDING**  
> **Destination System**: Bop ERP (Destination Platform)  
> **Source System**: Prophet 21 (Client Production ERP)  
> **Integration Direction**: Strictly Read-Only (Prophet 21 &rarr; Bop ERP)  
> **Security Policy**: Zero live production writes, zero data alteration, zero secret exchange via plain text.

---

## 1. Executive Summary & Purpose

To ensure a seamless, non-disruptive migration and master data synchronization, Bop ERP requires technical catalog metadata from your Prophet 21 database. 

**Our Guarantees to Client IT**:
1. **100% Read-Only**: Bop ERP will never write, update, delete, or alter any record in Prophet 21.
2. **Zero Business Data in Intake**: The initial schema intake extracts **only structural metadata** (table names, column types, keys, and row count estimates). No customer names, contact info, pricing, or financial transactions are captured.
3. **No Plaintext Passwords**: We will never ask for credentials via email, chat, or ticket.

---

## 2. Client IT Technical Parameters Questionnaire

Please complete the following table and return it to the Bop ERP migration engineering team:

| # | Parameter | Description / Clarification | Client IT Response |
| :-: | :--- | :--- | :--- |
| **1** | **P21 Version / Build** | e.g., 2021.2, 2023.1, Cloud vs On-Premises | |
| **2** | **SQL Server Version & Edition** | e.g., Microsoft SQL Server 2019 Standard (v15.x) | |
| **3** | **Database / Catalog Name** | Exact name of the Prophet 21 database | |
| **4** | **Database Server Timezone** | Server timezone & UTC offset (e.g., America/New_York, UTC-5) | |
| **5** | **Database Collation** | Default collation (e.g., `SQL_Latin1_General_CP1_CI_AS`) | |
| **6** | **Company Structure Overview** | High-level overview of legal companies operating in P21 | |
| **7** | **Company-Scope Mechanism** | How company tenancy is isolated (see Section 3 below) | |
| **8** | **Master Data Estimates** | Approximate counts for Customers, Vendors, Items, Warehouses | |
| **9** | **Preferred Access Method** | e.g., Restored DB Backup, Read-Only Replica, Direct Read-Only SQL | |
| **10** | **Backup / Snapshot Availability** | Frequency and availability of `.bak` database snapshots | |
| **11** | **Network & Security Requirements** | VPN required? Static IP Allowlist? Firewall restrictions? | |
| **12** | **Dedicated SELECT-Only Account** | Can a dedicated read-only SQL user be provisioned? (Yes/No) | |
| **13** | **Reporting / AG Replica** | Does an AlwaysOn or secondary reporting replica exist? | |
| **14** | **Metadata Execution Approval** | Approval to execute the provided read-only metadata SQL script? | |
| **15** | **Custom Extensions / UDFs** | Are there known custom tables, views, or user-defined fields? | |

---

## 3. Mandatory Company Scope Questionnaire

To prevent cross-company contamination, Bop ERP requires unambiguous evidence of how business entities are segregated. Please check the applicable architecture:

- [ ] **Single-Company Database**: The database catalog contains records exclusively for a single operating entity. No multi-tenant discriminator is used.
- [ ] **Separate Database per Company**: Multi-company setup where each legal operating company resides in its own discrete database catalog.
- [ ] **Explicit Company Discriminator Column**: Shared tables contain an explicit company ID column (e.g., `company_id`, `company_no`).
- [ ] **Relational Location/Branch/Company Mapping**: Entities belong to branches/locations that map relationally to specific companies.
- [ ] **Other Documented Mechanism**: Custom company isolation scheme (please describe below).

### Entity Mapping Specification:
| Source Company Identifier | Source Legal Entity Name | Target Bop Company | Notes / Scope |
| :--- | :--- | :--- | :--- |
| e.g., `1` or `CORP` | Industrial Depot Inc. | `Industrial DP` | Primary operating company |
| | | | |

> **IMPORTANT ARCHITECTURAL POLICY**: If company scope cannot be unambiguously confirmed, readiness status remains **`BLOCKED`**. Bop ERP fails closed to avoid data contamination.

---

## 4. Preferred Source Access Options

To eliminate operational risk to live ERP transactions, Bop ERP requests access through one of the following safe options:

### Option A: Restored Database Snapshot / Sanitized Copy (Highest Safety & Recommended)
- A standard Microsoft SQL Server native backup (`.bak`) restored into an isolated secondary staging/reporting environment.
- Eliminates 100% of CPU, memory, and lock contention on live ERP operations.
- Required provenance metadata:
  - Source instance identifier
  - Database name
  - Exact capture timestamp (ISO 8601)
  - Prophet 21 application version
  - SHA256 checksum of the backup file (if available)

### Option B: Dedicated Read-Only Reporting Replica (High Safety)
- SQL Server AlwaysOn Availability Group read-only secondary replica or log-shipped reporting server.
- Completely isolates query load from transactional users.

### Option C: Dedicated Read-Only SQL Account on Live Instance (Conditional)
- A dedicated SQL account restricted strictly to `CONNECT` and `SELECT` on required master tables/views.
- All queries will have enforced 60-second timeouts and bounded keyset batching.

---

## 5. Read-Only SQL Account Request Specification

If provisioning a dedicated SQL login for Bop ERP, the account **must conform to the least-privilege security contract**:

### Allowed Permissions:
- `CONNECT` to the designated database
- `SELECT` on specific master data tables or views (or `SCHEMA::dbo`)
- `VIEW DEFINITION` on the database/schema (for schema introspection)

### Strictly Forbidden Permissions:
The account **must NOT possess** any of the following roles or capabilities:
- `INSERT`, `UPDATE`, `DELETE`, `MERGE`
- `EXEC` / `EXECUTE` on any stored procedures
- `ALTER`, `CREATE`, `DROP`, `TRUNCATE`
- `CONTROL`
- Fixed database roles: `db_owner`, `db_datawriter`, `db_ddladmin`, `db_securityadmin`
- Server roles: `sysadmin`, `serveradmin`

*(Optional example for client DBA review)*:
```sql
-- OPTIONAL EXAMPLE FOR CLIENT DBA REVIEW
-- USE [p21_database];
-- CREATE LOGIN [bop_migration_ro] WITH PASSWORD = '<GeneratedSecurePassword>', CHECK_POLICY = ON;
-- CREATE USER [bop_migration_ro] FOR LOGIN [bop_migration_ro];
-- GRANT CONNECT TO [bop_migration_ro];
-- GRANT SELECT ON SCHEMA::dbo TO [bop_migration_ro];
-- GRANT VIEW DEFINITION TO [bop_migration_ro];
```

---

## 6. Offline Schema Metadata Capture Script

We have provided a standalone, 100% read-only SQL script:  
`docs/migration/sql/p21_schema_metadata_capture.sql`

### Safety Guarantees of the Script:
- **Pure SELECT Statements**: The script contains zero DDL, zero DML, and zero stored procedure executions.
- **System Catalogs Only**: Queries run against `sys.objects`, `sys.columns`, `sys.schemas`, `sys.indexes`, `sys.partitions`, and `sys.foreign_keys`.
- **Zero Business Row Data**: It returns table structures, column definitions, data types, nullability, primary key constraints, and row count estimates. It **never** selects rows from customer, item, order, or financial tables.

### Execution & Output:
Client IT can execute this script in SQL Server Management Studio (SSMS) or Azure Data Studio against the P21 database and export the results as JSON or CSV files.

---

## 7. Security & Credential Exchange Rules

> **CRITICAL SECURITY MANDATE**:
> - **NEVER** transmit database passwords, admin credentials, API tokens, or VPN passwords via email, unencrypted chat, or public ticket systems.
> - **NEVER** commit credentials, backups, or raw customer data to Git repositories.
> - If live/direct credentials are required in future phases, they must be transmitted through an approved end-to-end encrypted password manager (e.g., 1Password, Bitwarden, or encrypted one-time secret links).
