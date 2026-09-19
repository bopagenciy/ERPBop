# Phase 1Z / 1Z.3 Controlled Import Provenance Reconciliation

> **MIGRATION AUDIT & PROVENANCE CORRECTION RECORD**  
> **Repository**: ERPBop  
> **App**: bop_erp  
> **Status**: APPROVED / DOCUMENTED  
> **Date**: 2026-09-18  

---

## 1. Provenance Context & Clarification

### Previous Phase 1Z Report Description:
The Phase 1Z controlled import was initially described as selecting a deterministic slice of the first 15 valid ItemMaster records from client sample exports.

### Corrected Interpretation (Phases 1Z.2 & 1Z.3):
Audit of the physical client export workbook `1ItemMaster_sample.xlsx` established that:
1. **7 Items** in that previously selected test slice (`AB28400`, `AB28401`, `AB28402`, `AB28403`, `AB28404`, `AB28405`, and `AB39402`) **physically exist** in `1ItemMaster_sample.xlsx`, `5ItemDescription_sample.xlsx`, and `4ItemUnitofMeasure_sample.xlsx`.
2. **8 Items** in that previously selected slice (`AB28406`, `AB28407`, `AB28408`, `AB28409`, `AB28410`, `AB28411`, `AB28412`, and `AB28413`) were sequentially generated **synthetic test fixtures** that do not exist in any client sample workbooks.
3. Historical Git commit history is strictly preserved without rewriting.

---

## 2. Real-Sample Master Baseline Established (Phase 1Z.3)

To establish an unimpeachable master baseline where 100% of imported items originate from real client files, Phase 1Z.3 selected the first 15 real business rows from `1ItemMaster_sample.xlsx` after profile parsing:

| # | Item ID | ItemMaster Row | ItemDescription Row | ItemUOM Rows | Canonical Source Key | Source Data Class |
|:-:|:---|:---:|:---:|:---:|:---|:---:|
| 1 | `AB28400` | 5 | 6 | 6 | `["AB28400"]` | `CLIENT_SAMPLE` |
| 2 | `AB28401` | 6 | 7 | 7 | `["AB28401"]` | `CLIENT_SAMPLE` |
| 3 | `AB28402` | 7 | 8 | 8 | `["AB28402"]` | `CLIENT_SAMPLE` |
| 4 | `AB28403` | 8 | 9 | 9 | `["AB28403"]` | `CLIENT_SAMPLE` |
| 5 | `AB28404` | 9 | 10 | 10 | `["AB28404"]` | `CLIENT_SAMPLE` |
| 6 | `AB28405` | 10 | 11 | 11 | `["AB28405"]` | `CLIENT_SAMPLE` |
| 7 | `AB32176` | 11 | 12 | 12 | `["AB32176"]` | `CLIENT_SAMPLE` |
| 8 | `AB32182` | 12 | 13 | 13 | `["AB32182"]` | `CLIENT_SAMPLE` |
| 9 | `AB32185` | 13 | 14 | 14 | `["AB32185"]` | `CLIENT_SAMPLE` |
| 10 | `AB34036` | 14 | 15 | 15 | `["AB34036"]` | `CLIENT_SAMPLE` |
| 11 | `AB34130` | 15 | 16 | 16 | `["AB34130"]` | `CLIENT_SAMPLE` |
| 12 | `AB34131` | 16 | 17 | 17 | `["AB34131"]` | `CLIENT_SAMPLE` |
| 13 | `AB34141` | 17 | 18 | 18 | `["AB34141"]` | `CLIENT_SAMPLE` |
| 14 | `AB39400` | 18 | 19 | 19 | `["AB39400"]` | `CLIENT_SAMPLE` |
| 15 | `AB39402` | 19 | 20 | 20 | `["AB39402"]` | `CLIENT_SAMPLE` |

---

## 3. Data Class Separation Standard

Metadata and staging pipelines explicitly categorize source items via `SourceDataClass`:
- `CLIENT_SAMPLE`: Ingested directly from physical client workbooks (`local_data/p21_samples/`). Preserves exact file identifiers and physical source rows.
- `SYNTHETIC_FIXTURE`: Synthesized for unit, boundary, or edge testing. Forbidden from claiming client sample provenance.
