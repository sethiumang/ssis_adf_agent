"""Contoso Bank FSI packages 01-05: reference data, GL, custody, KYC, AML."""
from __future__ import annotations

from common import audit_vars, end_batch, oledb, on_error_logging, smtp, start_batch, yyyymmdd
from dtsx_builder import Package, T

STR, WSTR = 129, 130


# --------------------------------------------------------------------------- #
# 01 FX daily rates                                                           #
# --------------------------------------------------------------------------- #
def fx_daily_rates() -> tuple[Package, dict]:
    p = Package("FIN_FX_DailyRates_Load",
                "Loads daily noon-buying FX rates (Federal Reserve H.10 style CSV) into ref.FxRate for "
                "multi-currency GL revaluation and risk exposure conversion.")
    oledb(p, "FinanceDW", "ETLControl")
    bd = p.parameter("BusinessDate", "DateTime", "2026-01-14T00:00:00", required=True,
                     description="Business date of the rate file")
    audit_vars(p)
    p.variable("FxFilePath", "String", "",
               expression=r'@[$Project::InboundRoot] + "\\fx\\H10_" + ' + yyyymmdd(f"@[{bd}]") + ' + ".csv"')
    p.flat_file("FF_FxRates", r"\\fileshare01.contoso.local\etl\inbound\fx\H10_20260114.csv",
                [("RateDate", 10, STR), ("CurrencyCode", 3, STR), ("CurrencyName", 50, STR),
                 ("UnitsPerUsd", 20, STR), ("RateType", 10, STR)],
                expression="@[User::FxFilePath]", code_page=1252)

    s = start_batch(p, "FX daily rates")
    trunc = p.execute_sql("SQL Truncate Staging", "FinanceDW", "TRUNCATE TABLE stg.FxRate;")
    dft, df = p.data_flow("DFT Load FX Staging", "Straight file-to-table copy into stg.FxRate")
    src = df.flat_src("FF Source H10 Rates", "FF_FxRates",
                      [("RateDate", T.str(10)), ("CurrencyCode", T.str(3)), ("CurrencyName", T.str(50)),
                       ("UnitsPerUsd", T.str(20)), ("RateType", T.str(10))])
    df.ole_dest("OLE DST stg FxRate", src, "FinanceDW", "[stg].[FxRate]")
    merge = p.execute_sql(
        "SQL Merge ref FxRate", "FinanceDW",
        "MERGE ref.FxRate AS tgt\n"
        "USING (SELECT CONVERT(date, RateDate, 23) AS RateDate, CurrencyCode, CAST(UnitsPerUsd AS decimal(19,8)) AS UnitsPerUsd\n"
        "       FROM stg.FxRate WHERE RateType = 'NOON' AND ISNUMERIC(UnitsPerUsd) = 1) AS src\n"
        "   ON tgt.RateDate = src.RateDate AND tgt.CurrencyCode = src.CurrencyCode\n"
        "WHEN MATCHED AND tgt.UnitsPerUsd <> src.UnitsPerUsd THEN UPDATE SET UnitsPerUsd = src.UnitsPerUsd, ModifiedUtc = SYSUTCDATETIME()\n"
        "WHEN NOT MATCHED THEN INSERT (RateDate, CurrencyCode, UnitsPerUsd, ModifiedUtc) VALUES (src.RateDate, src.CurrencyCode, src.UnitsPerUsd, SYSUTCDATETIME());\n"
        "SELECT @@ROWCOUNT AS RowsLoaded;",
        result_type="SingleRow", results=[("RowsLoaded", "User::RowsLoaded")])
    check = p.execute_sql(
        "SQL Assert Currency Coverage", "FinanceDW",
        "IF (SELECT COUNT(*) FROM ref.FxRate WHERE RateDate = ? AND CurrencyCode IN ('EUR','GBP','JPY','CAD','CHF','MXN')) < 6\n"
        "    THROW 50001, 'FX coverage check failed: required G7/MXN currencies missing', 1;",
        params=[(bd, "Input", "DATE", "0")])
    e = end_batch(p)
    p.chain(s, trunc, dft, merge, check, e)
    return p, {
        "domain": "Finance / Reference data",
        "scenario": "Daily FX rate file ingestion and upsert",
        "regulatory_context": "Feeds GL revaluation and BCBS 239 exposure aggregation in a common currency",
        "schedule": "Daily 12:30 ET after H.10-style noon rates are published",
        "features": ["Project + package parameters", "Variable expression for dynamic file path",
                     "Flat file connection with ConnectionString expression", "Simple file->table data flow",
                     "Execute SQL MERGE with SingleRow result binding", "THROW-based data quality assertion",
                     "Batch audit start/end procs with OUTPUT parameter"],
        "expected_adf": ["Copy activity (DelimitedText -> Azure SQL)", "Script or Stored Procedure activity for MERGE",
                         "Lookup/Script activity for assertion", "Pipeline parameter BusinessDate"],
        "edge_cases": ["File name derived from date expression", "RateType filter applied after load, not in copy"],
        "complexity_hint": "Low",
    }


# --------------------------------------------------------------------------- #
# 02 GL incremental                                                           #
# --------------------------------------------------------------------------- #
def gl_incremental() -> tuple[Package, dict]:
    p = Package("FIN_GL_JournalEntries_Incremental",
                "Watermark-based incremental extract of general ledger journal lines from core banking "
                "into dw.FactGLJournalLine.")
    oledb(p, "CoreBanking", "FinanceDW", "ETLControl")
    audit_vars(p)
    p.variable("LastWatermarkUtc", "DateTime", "1/1/2026 12:00:00 AM")
    p.variable("NewWatermarkUtc", "DateTime", "1/1/2026 12:00:00 AM")

    wm = p.execute_sql(
        "SQL Get Watermark", "ETLControl",
        "SELECT LastWatermarkUtc, SYSUTCDATETIME() AS NewWatermarkUtc FROM etl.Watermark WHERE SourceTable = 'gl.JournalLine';",
        result_type="SingleRow",
        results=[("LastWatermarkUtc", "User::LastWatermarkUtc"), ("NewWatermarkUtc", "User::NewWatermarkUtc")])
    s = start_batch(p, "GL journal incremental")
    trunc = p.execute_sql("SQL Truncate Staging", "FinanceDW", "TRUNCATE TABLE stg.GLJournalLine;")
    dft, df = p.data_flow("DFT Extract Journal Lines")
    src = df.ole_src(
        "OLE SRC Journal Lines", "CoreBanking",
        "SELECT jh.JournalId, jl.LineNumber, jh.PostingDate, jh.JournalSource, jl.GlAccount, jl.CostCenter,\n"
        "       jl.LegalEntity, jl.CurrencyCode, jl.DebitAmount, jl.CreditAmount, jl.ModifiedUtc\n"
        "FROM gl.JournalHeader jh WITH (NOLOCK)\n"
        "JOIN gl.JournalLine jl WITH (NOLOCK) ON jl.JournalId = jh.JournalId\n"
        "WHERE jl.ModifiedUtc > ? AND jl.ModifiedUtc <= ? AND jh.Status = 'POSTED'",
        [("JournalId", T.I8), ("LineNumber", T.I4), ("PostingDate", T.DATE), ("JournalSource", T.wstr(20)),
         ("GlAccount", T.wstr(20)), ("CostCenter", T.wstr(10)), ("LegalEntity", T.wstr(10)),
         ("CurrencyCode", T.wstr(3)), ("DebitAmount", T.numeric(19, 4)), ("CreditAmount", T.numeric(19, 4)),
         ("ModifiedUtc", T.TIMESTAMP)],
        params=["User::LastWatermarkUtc", "User::NewWatermarkUtc"])
    der = df.derived("DER Audit Columns", src, [
        ("NetAmount", "ISNULL([DebitAmount]) ? (DT_NUMERIC,19,4)0 - [CreditAmount] : [DebitAmount] - (ISNULL([CreditAmount]) ? (DT_NUMERIC,19,4)0 : [CreditAmount])", T.numeric(19, 4)),
        ("BatchId", "@[User::BatchId]", T.I8),
        ("LoadDtsUtc", "(DT_DBTIMESTAMP)GETUTCDATE()", T.TIMESTAMP),
    ])
    rc = df.row_count("RC Rows Extracted", der, "User::RowsExtracted")
    df.ole_dest("OLE DST stg GLJournalLine", rc, "FinanceDW", "[stg].[GLJournalLine]")

    merge = p.execute_sql(
        "SQL Merge FactGLJournalLine", "FinanceDW",
        "EXEC dw.usp_MergeFactGLJournalLine @BatchId = ?;",
        params=[("User::BatchId", "Input", "BIGINT", "0")],
        description="MERGE on (JournalId, LineNumber); late-arriving reversals update NetAmount")
    upd_wm = p.execute_sql(
        "SQL Update Watermark", "ETLControl",
        "UPDATE etl.Watermark SET LastWatermarkUtc = ?, UpdatedBatchId = ? WHERE SourceTable = 'gl.JournalLine';",
        params=[("User::NewWatermarkUtc", "Input", "DBTIMESTAMP", "0"), ("User::BatchId", "Input", "BIGINT", "1")])
    e = end_batch(p)
    p.chain(wm, s, trunc, dft)
    p.constraint(dft, merge, expression="@[User::RowsExtracted] > 0", eval_op="ExpressionAndConstraint")
    p.constraint(merge, upd_wm, logical_and=False)
    p.constraint(dft, upd_wm, expression="@[User::RowsExtracted] == 0", eval_op="ExpressionAndConstraint",
                 logical_and=False)
    p.constraint(upd_wm, e)
    return p, {
        "domain": "Finance / General ledger",
        "scenario": "High-watermark incremental extract and MERGE into fact table",
        "regulatory_context": "Source of record for financial statements, FFIEC Call Report and SOX controls",
        "schedule": "Daily after core banking EOD (orchestrated by ORCH_EOD_Regulatory_Batch_Master)",
        "features": ["SingleRow result binding into DateTime variables", "Parameterized OLE DB source (? markers)",
                     "Derived Column with conditional/null handling", "Row Count into variable",
                     "Expression + constraint precedence", "OR (LogicalAnd=False) join of two branches",
                     "Stored procedure with OUTPUT parameter"],
        "expected_adf": ["Lookup activity for watermark", "Copy activity with parameterized source query",
                         "If Condition or dependency expression on rows copied",
                         "Stored Procedure activity for MERGE and watermark update"],
        "edge_cases": ["Zero-row run must still advance watermark (OR path)",
                       "Row Count variable drives control flow -> needs Copy output.rowsCopied in ADF",
                       "NOLOCK hints on source"],
        "complexity_hint": "Medium",
    }


# --------------------------------------------------------------------------- #
# 03 Custodian positions                                                      #
# --------------------------------------------------------------------------- #
def custodian_positions() -> tuple[Package, dict]:
    p = Package("OPS_Custodian_Positions_Ingest",
                "Loops over custodian position files (one per custodian per day), validates identifiers, "
                "enriches with security master and stages positions for IBOR/ABOR reconciliation.")
    oledb(p, "InvestmentOps", "ETLControl")
    audit_vars(p)
    p.variable("CurrentFile", "String", r"\\fileshare01.contoso.local\etl\inbound\custodian\POS_BNYM_20260114.csv")
    p.variable("ArchiveFolder", "String", "", expression=r'@[$Project::ArchiveRoot] + "\\custodian\\"')
    p.variable("ErrorFolder", "String", "", expression=r'@[$Project::ArchiveRoot] + "\\custodian\\error\\"')
    p.variable("RejectFile", "String", "",
               expression=r'@[$Project::ArchiveRoot] + "\\custodian\\rejects\\REJ_" + REPLACE(REPLACE(RIGHT(@[User::CurrentFile], FINDSTRING(REVERSE(@[User::CurrentFile]), "\\", 1) - 1), ".csv", ""), " ", "") + ".csv"')
    p.flat_file("FF_CustodianPositions", r"\\fileshare01.contoso.local\etl\inbound\custodian\POS_BNYM_20260114.csv",
                [("AccountNumber", 20, STR), ("Isin", 12, STR), ("Cusip", 9, STR), ("SecurityDescription", 100, STR),
                 ("Quantity", 30, STR), ("MarketValueLocal", 30, STR), ("Currency", 3, STR), ("AsOfDate", 10, STR)],
                expression="@[User::CurrentFile]", code_page=1252)
    p.flat_file("FF_PositionRejects", r"\\fileshare01.contoso.local\etl\archive\custodian\rejects\REJ.csv",
                [("AccountNumber", 20, STR), ("Isin", 12, STR), ("Quantity", 30, STR), ("SourceFile", 260, WSTR)],
                expression="@[User::RejectFile]", code_page=1252)

    s = start_batch(p, "Custodian positions")
    loop = p.foreach_file("FELC Custodian Files", folder=r"\\fileshare01.contoso.local\etl\inbound\custodian",
                          file_spec="POS_*_*.csv", mappings=["User::CurrentFile"],
                          folder_expression=r'@[$Project::InboundRoot] + "\\custodian"')
    reg = loop.execute_sql("SQL Register File", "ETLControl",
                           "EXEC etl.usp_RegisterInboundFile @BatchId = ?, @FilePath = ?;",
                           params=[("User::BatchId", "Input", "BIGINT", "0"),
                                   ("User::CurrentFile", "Input", "NVARCHAR", "1")])
    dft, df = loop.data_flow("DFT Stage Positions")
    src = df.flat_src("FF Source Positions", "FF_CustodianPositions",
                      [("AccountNumber", T.str(20)), ("Isin", T.str(12)), ("Cusip", T.str(9)),
                       ("SecurityDescription", T.str(100)), ("Quantity", T.str(30)),
                       ("MarketValueLocal", T.str(30)), ("Currency", T.str(3)), ("AsOfDate", T.str(10))])
    der = df.derived("DER Normalize", src, [
        ("IsinClean", "UPPER(TRIM([Isin]))", T.str(12)),
        ("QuantityNum", "(DT_NUMERIC,28,8)TRIM([Quantity])", T.numeric(28, 8)),
        ("MarketValueNum", "(DT_NUMERIC,19,4)TRIM([MarketValueLocal])", T.numeric(19, 4)),
        ("PositionDate", "(DT_DBDATE)[AsOfDate]", T.DATE),
        ("SourceFile", "@[User::CurrentFile]", T.wstr(260)),
    ])
    split = df.cond_split("CSPL Validate", der, [
        ("Invalid Identifier", "ISNULL([IsinClean]) || LEN([IsinClean]) != 12"),
        ("Zero Quantity", "[QuantityNum] == 0"),
    ], default_name="Valid Positions")
    match, no_match = df.lookup(
        "LKP Security Master", split["Valid Positions"], "InvestmentOps",
        "SELECT Isin, SecurityId, AssetClass FROM ref.SecurityMaster WHERE IsActive = 1",
        joins=[("IsinClean", "Isin")],
        copy=[("SecurityId", "SecurityId", T.I4), ("AssetClass", "AssetClass", T.wstr(20))])
    rc_loaded = df.row_count("RC Rows Loaded", match, "User::RowsLoaded")
    df.ole_dest("OLE DST stg CustodianPosition", rc_loaded, "InvestmentOps", "[stg].[CustodianPosition]",
                mapping=[("AccountNumber", "AccountNumber"), ("IsinClean", "Isin"), ("Cusip", "Cusip"),
                         ("SecurityId", "SecurityId"), ("AssetClass", "AssetClass"), ("QuantityNum", "Quantity"),
                         ("MarketValueNum", "MarketValueLocal"), ("Currency", "Currency"),
                         ("PositionDate", "PositionDate"), ("SourceFile", "SourceFile")])
    union = df.union_all("UALL Rejects", [split["Invalid Identifier"], no_match],
                         [("AccountNumber", T.str(20)), ("Isin", T.str(12)), ("Quantity", T.str(30)),
                          ("SourceFile", T.wstr(260))])
    rc_rej = df.row_count("RC Rows Rejected", union, "User::RowsRejected")
    df.flat_dest("FF DST Rejects", rc_rej, "FF_PositionRejects")
    # NOTE: "Zero Quantity" output is intentionally left unconnected (silent row drop).

    archive = loop.file_system("FST Archive File", "MoveFile", source="User::CurrentFile", source_is_var=True,
                               destination="User::ArchiveFolder", destination_is_var=True)
    quarantine = loop.file_system("FST Quarantine File", "MoveFile", source="User::CurrentFile",
                                  source_is_var=True, destination="User::ErrorFolder", destination_is_var=True)
    loop.chain(reg, dft, archive)
    loop.constraint(dft, quarantine, "Failure")

    publish = p.execute_sql("SQL Publish Positions", "InvestmentOps",
                            "EXEC ibor.usp_PublishCustodianPositions @BatchId = ?;",
                            params=[("User::BatchId", "Input", "BIGINT", "0")],
                            description="Promote staged positions and run IBOR vs custodian break detection")
    e = end_batch(p)
    p.chain(s, loop, publish, e)
    return p, {
        "domain": "Investment operations / Custody",
        "scenario": "Multi-file custodian position ingestion with validation, enrichment and rejects",
        "regulatory_context": "Supports SEC Rule 17a-13 style position reconciliation and NAV oversight",
        "schedule": "Daily 06:00 ET once custodian SFTP drops land",
        "features": ["Foreach File enumerator with folder expression", "Flat file CM driven by loop variable",
                     "Derived Column type casts", "Conditional Split (3 outputs)", "Lookup with no-match output",
                     "Union All of reject streams", "Row Count", "Flat file destination for rejects",
                     "File System Task move on success / quarantine on failure (Failure constraint)"],
        "expected_adf": ["Get Metadata + ForEach", "Mapping Data Flow (derive, split, lookup, union)",
                         "Copy/Delete activities or Azure Function for file move", "Stored Procedure activity"],
        "edge_cases": ["'Zero Quantity' split output is unconnected -> rows silently dropped; reconciliation must catch it",
                       "Failure path inside loop: SSIS loop fails after quarantine unless MaximumErrorCount raised",
                       "UNC paths must be re-pointed to ADLS/Blob"],
        "complexity_hint": "High",
    }


# --------------------------------------------------------------------------- #
# 04 Customer KYC SCD2                                                        #
# --------------------------------------------------------------------------- #
def customer_kyc_scd2() -> tuple[Package, dict]:
    p = Package("CRM_Customer_KYC_SCD2",
                "Maintains dw.DimCustomer as a Type 2 slowly changing dimension, tracking KYC risk rating, "
                "PEP flag, segment and country history for AML and conduct analytics.")
    oledb(p, "CRM", "FinanceDW", "ETLControl")
    bd = p.parameter("BusinessDate", "DateTime", "2026-01-14T00:00:00", required=True)
    audit_vars(p)

    s = start_batch(p, "Customer KYC SCD2")
    seq = p.sequence("SEQ Load DimCustomer")
    dft, df = seq.data_flow("DFT DimCustomer SCD2")
    src = df.ole_src(
        "OLE SRC CRM Customers", "CRM",
        "SELECT c.CustomerNumber, c.FirstName, c.LastName, c.DateOfBirth, c.TaxIdLast4, c.Email,\n"
        "       c.Segment, k.RiskRating AS KycRiskRating, k.IsPep AS PepFlag, c.CountryOfResidence, c.ModifiedDate\n"
        "FROM crm.Customer c\n"
        "JOIN kyc.CustomerRiskProfile k ON k.CustomerNumber = c.CustomerNumber AND k.IsCurrent = 1\n"
        "WHERE c.ModifiedDate >= DATEADD(day, -1, ?) OR k.ReviewedDate >= DATEADD(day, -1, ?)",
        [("CustomerNumber", T.wstr(20)), ("FirstName", T.wstr(50)), ("LastName", T.wstr(80)),
         ("DateOfBirth", T.DATE), ("TaxIdLast4", T.wstr(4)), ("Email", T.wstr(256)), ("Segment", T.wstr(20)),
         ("KycRiskRating", T.wstr(10)), ("PepFlag", T.BOOL), ("CountryOfResidence", T.wstr(2)),
         ("ModifiedDate", T.TIMESTAMP)],
        params=[bd, bd])
    der = df.derived("DER PII Masking", src, [
        ("FullName", "TRIM([FirstName]) + \" \" + TRIM([LastName])", T.wstr(131)),
        ("TaxIdMasked", "\"***-**-\" + [TaxIdLast4]", T.wstr(11)),
    ])
    outs = df.scd("SCD DimCustomer", der, "FinanceDW", "[dw].[DimCustomer]",
                  business_keys=["CustomerNumber"], changing=["FullName", "Email", "TaxIdMasked"],
                  historical=["KycRiskRating", "PepFlag", "Segment", "CountryOfResidence"],
                  fixed=["DateOfBirth"])
    df.ole_cmd("OLE CMD Type1 Update", outs["Changing Attribute Updates Output"], "FinanceDW",
               "UPDATE dw.DimCustomer SET FullName = ?, Email = ?, TaxIdMasked = ? WHERE CustomerNumber = ?",
               ["FullName", "Email", "TaxIdMasked", "CustomerNumber"])
    hist_der = df.derived("DER Expire Date", outs["Historical Attribute Inserts Output"], [
        ("ExpiryDate", "(DT_DBTIMESTAMP)@[$Package::BusinessDate]", T.TIMESTAMP)])
    expired = df.ole_cmd("OLE CMD Expire Current Row", hist_der, "FinanceDW",
                         "UPDATE dw.DimCustomer SET IsCurrent = 0, EffectiveTo = ? WHERE CustomerNumber = ? AND IsCurrent = 1",
                         ["ExpiryDate", "CustomerNumber"])
    cols = [("CustomerNumber", T.wstr(20)), ("FullName", T.wstr(131)), ("DateOfBirth", T.DATE),
            ("TaxIdMasked", T.wstr(11)), ("Email", T.wstr(256)), ("Segment", T.wstr(20)),
            ("KycRiskRating", T.wstr(10)), ("PepFlag", T.BOOL), ("CountryOfResidence", T.wstr(2))]
    union = df.union_all("UALL New And Historical", [outs["New Output"], expired], cols)
    eff = df.derived("DER Effective Dates", union, [
        ("EffectiveFrom", "(DT_DBTIMESTAMP)@[$Package::BusinessDate]", T.TIMESTAMP),
        ("IsCurrent", "(DT_BOOL)1", T.BOOL)])
    rc = df.row_count("RC Rows Inserted", eff, "User::RowsLoaded")
    df.ole_dest("OLE DST DimCustomer Insert", rc, "FinanceDW", "[dw].[DimCustomer]")

    dq = seq.execute_sql(
        "SQL KYC Data Quality Metrics", "FinanceDW",
        "INSERT INTO dq.MetricResult (BatchId, MetricName, MetricValue, Threshold, Passed)\n"
        "SELECT ?, 'DimCustomer.CurrentRowsWithoutRiskRating', COUNT(*), 0, CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END\n"
        "FROM dw.DimCustomer WHERE IsCurrent = 1 AND KycRiskRating IS NULL\n"
        "UNION ALL\n"
        "SELECT ?, 'DimCustomer.DuplicateCurrentRows', COUNT(*), 0, CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END\n"
        "FROM (SELECT CustomerNumber FROM dw.DimCustomer WHERE IsCurrent = 1 GROUP BY CustomerNumber HAVING COUNT(*) > 1) d;",
        params=[("User::BatchId", "Input", "BIGINT", "0"), ("User::BatchId", "Input", "BIGINT", "1")])
    seq.chain(dft, dq)
    e = end_batch(p)
    p.chain(s, seq, e)
    return p, {
        "domain": "Customer / KYC",
        "scenario": "Type 2 slowly changing customer dimension with PII masking and KYC history",
        "regulatory_context": "BSA/AML CDD rule (31 CFR 1010.230) customer risk profile history; GLBA PII handling",
        "schedule": "Daily",
        "features": ["Sequence container", "SCD wizard component (Microsoft.SCD)",
                     "OLE DB Command (row-by-row update)", "Union All", "Derived Column using package parameter",
                     "PII columns (DOB, tax id, email)", "Data quality metrics SQL"],
        "expected_adf": ["Mapping Data Flow with Alter Row (upsert/expire) or Synapse/Azure SQL MERGE proc",
                         "Sequence flattened into dependsOn chain"],
        "edge_cases": ["SCD component has no ADF equivalent -> must be redesigned",
                       "OLE DB Command is row-by-row; set-based rewrite changes ordering semantics",
                       "PII must be classified and masked in the target"],
        "complexity_hint": "High",
    }


# --------------------------------------------------------------------------- #
# 05 AML CTR monitoring                                                       #
# --------------------------------------------------------------------------- #
CTR_SCRIPT = r'''#region Namespaces
using System;
using System.Data;
using System.Data.SqlClient;
using Microsoft.SqlServer.Dts.Runtime;
#endregion

namespace ST_CtrExemptions
{
    [Microsoft.SqlServer.Dts.Tasks.ScriptTask.SSISScriptTaskEntryPointAttribute]
    public partial class ScriptMain : Microsoft.SqlServer.Dts.Tasks.ScriptTask.VSTARTScriptObjectModelBase
    {
        // Applies Phase I / Phase II exemptions (31 CFR 1020.315) and flags possible structuring:
        // same customer, multiple branches, each deposit just under the threshold within the business day.
        public void Main()
        {
            long batchId = (long)Dts.Variables["User::BatchId"].Value;
            DateTime businessDate = (DateTime)Dts.Variables["$Package::BusinessDate"].Value;
            decimal threshold = Convert.ToDecimal(Dts.Variables["$Project::CtrThresholdUsd"].Value);

            var cm = Dts.Connections["Compliance"];
            var conn = (SqlConnection)cm.AcquireConnection(Dts.Transaction);
            try
            {
                using (var cmd = new SqlCommand(@"
                    UPDATE c SET c.IsExempt = 1, c.ExemptionReason = e.ExemptionType
                    FROM cmp.CtrCandidate c
                    JOIN cmp.CtrExemptPerson e ON e.CustomerId = c.CustomerId AND e.EffectiveTo IS NULL
                    WHERE c.BatchId = @BatchId;

                    INSERT INTO cmp.StructuringAlert (BatchId, CustomerId, BusinessDate, BranchCount, TotalCash)
                    SELECT @BatchId, w.CustomerId, @BusinessDate, COUNT(DISTINCT w.BranchId), SUM(w.TotalCashAmount)
                    FROM cmp.StructuringWatch w
                    WHERE w.BatchId = @BatchId AND w.TotalCashAmount BETWEEN @Threshold * 0.8 AND @Threshold
                    GROUP BY w.CustomerId HAVING COUNT(DISTINCT w.BranchId) >= 2;

                    SELECT COUNT(*) FROM cmp.CtrCandidate WHERE BatchId = @BatchId AND IsExempt = 0;", conn))
                {
                    cmd.Parameters.AddWithValue("@BatchId", batchId);
                    cmd.Parameters.AddWithValue("@BusinessDate", businessDate);
                    cmd.Parameters.AddWithValue("@Threshold", threshold);
                    Dts.Variables["User::CtrCandidates"].Value = Convert.ToInt32(cmd.ExecuteScalar());
                }
                Dts.TaskResult = (int)ScriptResults.Success;
            }
            catch (Exception ex)
            {
                Dts.Events.FireError(0, "CTR Exemptions", ex.Message, string.Empty, 0);
                Dts.TaskResult = (int)ScriptResults.Failure;
            }
            finally
            {
                cm.ReleaseConnection(conn);
            }
        }

        enum ScriptResults
        {
            Success = Microsoft.SqlServer.Dts.Runtime.DTSExecResult.Success,
            Failure = Microsoft.SqlServer.Dts.Runtime.DTSExecResult.Failure
        };
    }
}
'''


def aml_ctr_monitoring() -> tuple[Package, dict]:
    p = Package("AML_CTR_Daily_Monitoring",
                "Aggregates daily cash-in/cash-out per customer, identifies Currency Transaction Report "
                "candidates over the $10,000 threshold, applies exemptions and structuring rules, and "
                "notifies the BSA officer.")
    oledb(p, "CoreBanking", "Compliance", "ETLControl")
    p.oledb("Compliance_ADO", "CMPSQL01.contoso.local", "Compliance")
    smtp(p)
    bd = p.parameter("BusinessDate", "DateTime", "2026-01-14T00:00:00", required=True)
    audit_vars(p)
    p.variable("CtrCandidates", "Int32", "0")

    s = start_batch(p, "AML CTR monitoring")
    clean = p.execute_sql("SQL Clear Day", "Compliance",
                          "DELETE FROM cmp.CtrCandidate WHERE BusinessDate = ?; DELETE FROM cmp.StructuringWatch WHERE BusinessDate = ?;",
                          params=[(bd, "Input", "DATE", "0"), (bd, "Input", "DATE", "1")])
    dft, df = p.data_flow("DFT Aggregate Cash Activity")
    src = df.ole_src(
        "OLE SRC Cash Transactions", "CoreBanking",
        "SELECT t.CustomerId, t.AccountNumber, CAST(t.PostedDate AS date) AS BusinessDate, t.CashDirection,\n"
        "       t.Amount, t.BranchId\n"
        "FROM dep.Transaction t\n"
        "WHERE t.IsCash = 1 AND t.PostedDate >= ? AND t.PostedDate < DATEADD(day, 1, ?)\n"
        "  AND t.ReversalOfTransactionId IS NULL",
        [("CustomerId", T.I8), ("AccountNumber", T.wstr(20)), ("BusinessDate", T.DATE),
         ("CashDirection", T.wstr(3)), ("Amount", T.numeric(19, 4)), ("BranchId", T.I4)],
        params=[bd, bd])
    agg = df.aggregate("AGG Cash Per Customer Day", src, [
        ("CustomerId", "GroupBy", "CustomerId", T.I8),
        ("BusinessDate", "GroupBy", "BusinessDate", T.DATE),
        ("CashDirection", "GroupBy", "CashDirection", T.wstr(3)),
        ("TotalCashAmount", "Sum", "Amount", T.numeric(19, 4)),
        ("TransactionCount", "CountAll", None, T.I8),
        ("BranchCount", "CountDistinct", "BranchId", T.I8),
    ])
    split = df.cond_split("CSPL CTR Threshold", agg, [
        ("Reportable", "[TotalCashAmount] > @[$Project::CtrThresholdUsd]"),
        ("Near Threshold", "[TotalCashAmount] >= @[$Project::CtrThresholdUsd] * 0.8"),
    ], default_name="Below Threshold")
    mc = df.multicast("MC Reportable", split["Reportable"], 2)
    batch_der = df.derived("DER Batch", mc[0], [("BatchId", "@[User::BatchId]", T.I8)])
    df.ole_dest("OLE DST CtrCandidate", batch_der, "Compliance", "[cmp].[CtrCandidate]")
    agg2 = df.aggregate("AGG Daily CTR Totals", mc[1], [
        ("BusinessDate", "GroupBy", "BusinessDate", T.DATE),
        ("CandidateCount", "CountAll", None, T.I8),
        ("TotalReportableCash", "Sum", "TotalCashAmount", T.numeric(19, 4))])
    df.ole_dest("OLE DST CtrDailySummary", agg2, "Compliance", "[cmp].[CtrDailySummary]")
    near = df.derived("DER Batch Near", split["Near Threshold"], [("BatchId", "@[User::BatchId]", T.I8)])
    df.ole_dest("OLE DST StructuringWatch", near, "Compliance", "[cmp].[StructuringWatch]")

    script = p.script_task("SCR Apply Exemptions And Structuring", CTR_SCRIPT,
                           read_only="User::BatchId,$Package::BusinessDate,$Project::CtrThresholdUsd",
                           read_write="User::CtrCandidates",
                           description="C#: CTR exemptions + structuring heuristics")
    worklist = p.execute_sql("SQL Build FinCEN Worklist", "Compliance",
                             "EXEC cmp.usp_BuildCtrFilingWorklist @BatchId = ?, @BusinessDate = ?;",
                             params=[("User::BatchId", "Input", "BIGINT", "0"), (bd, "Input", "DATE", "1")],
                             description="Queue CTRs for BSA E-Filing within 15 days")
    mail = p.send_mail("MAIL Notify BSA Officer", "SMTP_Contoso", sender="aml-batch@contoso.example",
                       to="bsa.officer@contoso.example", subject="CTR candidates ready for review",
                       body="CTR candidates were identified for the business date. Review the cmp.CtrFilingWorklist queue.",
                       priority="High")
    mail.expr("ToLine", "@[$Project::BsaOfficerEmail]")
    mail.expr("Subject", '"[AML] " + (DT_WSTR,10)@[User::CtrCandidates] + " CTR candidates for " + (DT_WSTR,30)@[$Package::BusinessDate]')
    e = end_batch(p)
    p.chain(s, clean, dft, script, worklist)
    p.constraint(worklist, mail, expression="@[User::CtrCandidates] > 0", eval_op="ExpressionAndConstraint")
    p.constraint(worklist, e)
    on_error_logging(p)
    return p, {
        "domain": "Compliance / AML",
        "scenario": "Daily Currency Transaction Report candidate detection",
        "regulatory_context": "31 CFR 1010.311 CTR >$10,000; 31 CFR 1020.315 exemptions; FinCEN BSA E-Filing",
        "schedule": "Daily after core EOD",
        "features": ["Aggregate with GroupBy/Sum/CountAll/CountDistinct", "Conditional Split with project parameter",
                     "Multicast", "C# Script Task with ADO.NET and connection manager",
                     "Send Mail with property expressions", "Expression precedence constraint",
                     "OnError event handler logging"],
        "expected_adf": ["Mapping Data Flow (aggregate, split, new branch)",
                         "Azure Function or Stored Procedure replacing Script Task",
                         "Logic App / Web activity for email", "Pipeline failure path for OnError"],
        "edge_cases": ["'Below Threshold' output intentionally unused",
                       "Script Task embeds business rules; exact SQL must be preserved",
                       "Threshold is project parameter, comparisons must keep decimal precision"],
        "complexity_hint": "High",
    }
