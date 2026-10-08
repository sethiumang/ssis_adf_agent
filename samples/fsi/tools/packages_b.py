"""Contoso Bank FSI packages 06-10: payments, treasury, risk, sanctions, orchestration."""
from __future__ import annotations

from common import audit_vars, end_batch, oledb, on_error_logging, smtp, start_batch, yyyymmdd
from dtsx_builder import Package, T

STR, WSTR = 129, 130


# --------------------------------------------------------------------------- #
# 06 ACH NACHA outbound                                                       #
# --------------------------------------------------------------------------- #
NACHA_COMPONENT = r'''using System;
using Microsoft.SqlServer.Dts.Pipeline.Wrapper;
using Microsoft.SqlServer.Dts.Runtime.Wrapper;

[Microsoft.SqlServer.Dts.Pipeline.SSISScriptComponentEntryPointAttribute]
public class ScriptMain : UserComponent
{
    // Formats ACH entries into fixed-width 94-character NACHA records:
    // 1 File Header, 5 Batch Header, 6 Entry Detail, 8 Batch Control, 9 File Control (+ 9999 padding).
    private long _entryHash;
    private long _totalDebit;
    private long _totalCredit;
    private int _entryCount;

    public override void Input0_ProcessInputRow(Input0Buffer Row)
    {
        string record;
        switch (Row.RecordTypeCode)
        {
            case "6":
                record = "6"
                    + Row.TransactionCode.PadLeft(2, '0')
                    + Row.RoutingNumber.Substring(0, 8)
                    + Row.RoutingNumber.Substring(8, 1)
                    + Row.AccountNumber.PadRight(17)
                    + Row.AmountCents.ToString().PadLeft(10, '0')
                    + Row.IndividualId.PadRight(15)
                    + Row.IndividualName.PadRight(22).Substring(0, 22)
                    + "  "
                    + "0"
                    + Row.TraceNumber.PadLeft(15, '0');
                _entryHash += long.Parse(Row.RoutingNumber.Substring(0, 8));
                _entryCount++;
                if (Row.TransactionCode == "27" || Row.TransactionCode == "37") _totalDebit += Row.AmountCents;
                else _totalCredit += Row.AmountCents;
                break;
            default:
                record = Row.PrebuiltRecord;
                break;
        }

        if (record.Length != 94)
        {
            bool cancel;
            ComponentMetaData.FireError(0, "NACHA", "Record length " + record.Length + " for trace " + Row.TraceNumber, "", 0, out cancel);
        }
        Output0Buffer.AddRow();
        Output0Buffer.NachaRecord = record;
    }
}
'''

SFTP_SCRIPT = r'''#region Namespaces
using System;
using System.IO;
using Microsoft.SqlServer.Dts.Runtime;
#endregion

namespace ST_WinScpScript
{
    [Microsoft.SqlServer.Dts.Tasks.ScriptTask.SSISScriptTaskEntryPointAttribute]
    public partial class ScriptMain : Microsoft.SqlServer.Dts.Tasks.ScriptTask.VSTARTScriptObjectModelBase
    {
        // Writes a transient WinSCP script; the SFTP password is a sensitive project parameter
        // and therefore cannot be bound directly to the Execute Process Arguments property.
        public void Main()
        {
            string host = (string)Dts.Variables["$Project::SftpHost"].Value;
            string user = (string)Dts.Variables["$Project::SftpUser"].Value;
            string password = Dts.Variables["$Project::SftpPassword"].GetSensitiveValue().ToString();
            string file = (string)Dts.Variables["User::EncryptedFile"].Value;
            string scriptPath = (string)Dts.Variables["User::WinScpScriptPath"].Value;

            File.WriteAllLines(scriptPath, new[]
            {
                "option batch abort",
                "option confirm off",
                "open sftp://" + Uri.EscapeDataString(user) + ":" + Uri.EscapeDataString(password) + "@" + host + "/ -hostkey=\"ssh-ed25519 255 FAKEHOSTKEYFINGERPRINT\"",
                "cd /inbound/ach",
                "put \"" + file + "\"",
                "exit"
            });
            Dts.TaskResult = (int)ScriptResults.Success;
        }

        enum ScriptResults
        {
            Success = Microsoft.SqlServer.Dts.Runtime.DTSExecResult.Success,
            Failure = Microsoft.SqlServer.Dts.Runtime.DTSExecResult.Failure
        };
    }
}
'''


def ach_nacha_outbound() -> tuple[Package, dict]:
    p = Package("PAY_ACH_NACHA_Outbound",
                "Builds NACHA-formatted ACH origination files per company batch, PGP-encrypts them and "
                "transmits via SFTP to the ACH operator, then marks batches as transmitted.",
                protection_level=1)
    oledb(p, "CoreBanking", "ETLControl")
    smtp(p)
    audit_vars(p)
    p.variable("PendingBatches", "Object", "", description="ADO recordset of pending ACH batches")
    p.variable("CurrentAchBatchId", "Int32", "0")
    p.variable("CurrentCompanyId", "String", "")
    p.variable("NachaFile", "String", "",
               expression=r'@[$Project::OutboundRoot] + "\\ach\\ACH_" + @[User::CurrentCompanyId] + "_" + (DT_WSTR,10)@[User::CurrentAchBatchId] + ".txt"')
    p.variable("EncryptedFile", "String", "", expression='@[User::NachaFile] + ".pgp"')
    p.variable("WinScpScriptPath", "String", "",
               expression=r'@[$Project::OutboundRoot] + "\\ach\\winscp_" + (DT_WSTR,10)@[User::CurrentAchBatchId] + ".txt"')
    p.variable("SentFolder", "String", "", expression=r'@[$Project::ArchiveRoot] + "\\ach\\sent\\"')
    p.flat_file("FF_NachaOut", r"\\fileshare01.contoso.local\etl\outbound\ach\ACH.txt",
                [("NachaRecord", 94, STR)], expression="@[User::NachaFile]", header=False,
                text_qualifier=None, code_page=1252)

    s = start_batch(p, "ACH NACHA outbound")
    get = p.execute_sql("SQL Get Pending ACH Batches", "CoreBanking",
                        "SELECT AchBatchId, CompanyId FROM pay.AchBatch WHERE Status = 'APPROVED' AND EffectiveEntryDate <= DATEADD(day, 1, CAST(GETDATE() AS date)) ORDER BY AchBatchId;",
                        result_type="Rowset", results=[("0", "User::PendingBatches")])
    loop = p.foreach_ado("FELC Each ACH Batch", source_variable="User::PendingBatches",
                         mappings=["User::CurrentAchBatchId", "User::CurrentCompanyId"])
    dft, df = loop.data_flow("DFT Build NACHA File")
    src = df.ole_src("OLE SRC ACH Records", "CoreBanking", "EXEC pay.usp_GetAchFileRecords @AchBatchId = ?",
                     [("RecordSeq", T.I4), ("RecordTypeCode", T.wstr(1)), ("TransactionCode", T.wstr(2)),
                      ("RoutingNumber", T.wstr(9)), ("AccountNumber", T.wstr(17)), ("AmountCents", T.I8),
                      ("IndividualId", T.wstr(15)), ("IndividualName", T.wstr(22)), ("TraceNumber", T.wstr(15)),
                      ("PrebuiltRecord", T.wstr(94))],
                     params=["User::CurrentAchBatchId"])
    sc = df.script_component("SC Format NACHA Records", src, NACHA_COMPONENT, [("NachaRecord", T.str(94))],
                             project_name="SC_NachaFormatter")
    df.flat_dest("FF DST NACHA File", sc, "FF_NachaOut", columns=["NachaRecord"])

    gpg = loop.execute_process("EPT PGP Encrypt", executable=r"C:\Program Files (x86)\GnuPG\bin\gpg.exe",
                               arguments="--batch --yes", working_dir=r"C:\ETL\ach", timeout=300)
    gpg.expr("Arguments", r'"--batch --yes --trust-model always -r " + @[$Project::PgpRecipient] + " -o \"" + @[User::EncryptedFile] + "\" -e \"" + @[User::NachaFile] + "\""')
    winscp_script = loop.script_task("SCR Write WinSCP Script", SFTP_SCRIPT,
                                     read_only="$Project::SftpHost,$Project::SftpUser,$Project::SftpPassword,User::EncryptedFile,User::WinScpScriptPath",
                                     description="Writes a WinSCP script using the sensitive SFTP password")
    sftp = loop.execute_process("EPT SFTP Upload", executable=r"C:\Program Files (x86)\WinSCP\WinSCP.com",
                                arguments="/ini=nul", working_dir=r"C:\ETL\ach", timeout=600)
    sftp.expr("Arguments", r'"/ini=nul /log=C:\\ETL\\ach\\winscp.log /script=\"" + @[User::WinScpScriptPath] + "\""')
    cleanup = loop.file_system("FST Delete WinSCP Script", "DeleteFile", source="User::WinScpScriptPath",
                               source_is_var=True)
    archive = loop.file_system("FST Archive Clear Text", "MoveFile", source="User::NachaFile", source_is_var=True,
                               destination="User::SentFolder", destination_is_var=True)
    mark = loop.execute_sql("SQL Mark Transmitted", "CoreBanking",
                            "UPDATE pay.AchBatch SET Status = 'TRANSMITTED', TransmittedUtc = SYSUTCDATETIME(), FileName = ? WHERE AchBatchId = ?;",
                            params=[("User::EncryptedFile", "Input", "NVARCHAR", "0"),
                                    ("User::CurrentAchBatchId", "Input", "LONG", "1")])
    fail_mail = loop.send_mail("MAIL ACH Transmission Failed", "SMTP_Contoso", sender="ach-batch@contoso.example",
                               to="etl-ops@contoso.example", subject="ACH transmission FAILED",
                               body="SFTP upload to the ACH operator failed. Cutoff risk - escalate immediately.",
                               priority="High")
    fail_mail.expr("Subject", '"ACH transmission FAILED for batch " + (DT_WSTR,10)@[User::CurrentAchBatchId]')
    loop.chain(dft, gpg, winscp_script, sftp, cleanup, archive, mark)
    loop.constraint(sftp, fail_mail, "Failure")
    e = end_batch(p)
    p.chain(s, get, loop, e)
    return p, {
        "domain": "Payments / ACH",
        "scenario": "Outbound NACHA file generation, encryption and SFTP transmission per batch",
        "regulatory_context": "Nacha Operating Rules file format; 2026 fraud monitoring rule phases; cutoff SLAs",
        "schedule": "Multiple windows per day ahead of ACH operator cutoffs",
        "features": ["Full result set into Object variable", "Foreach ADO enumerator with variable mappings",
                     "Script Component (C#) generating fixed-width records", "Single-column flat file destination",
                     "Execute Process (gpg, WinSCP) with Arguments expressions",
                     "Script Task reading sensitive parameter via GetSensitiveValue",
                     "File System delete + move", "ProtectionLevel=EncryptSensitiveWithUserKey"],
        "expected_adf": ["Lookup + ForEach", "Azure Function for NACHA formatting/PGP",
                         "SFTP linked service + Copy activity (Key Vault secret)", "Stored Procedure activity"],
        "edge_cases": ["Local executables (gpg.exe, WinSCP.com) do not exist in ADF",
                       "Sensitive parameter must move to Key Vault",
                       "Byte-exact output: reconciliation must compare file hash/length, not just row counts",
                       "ProtectionLevel=1 means sensitive values unreadable on another machine"],
        "complexity_hint": "Very High",
    }


# --------------------------------------------------------------------------- #
# 07 Bank statement reconciliation                                            #
# --------------------------------------------------------------------------- #
def bank_statement_recon() -> tuple[Package, dict]:
    p = Package("TRS_Bank_Statement_Reconciliation",
                "Reconciles parsed bank statement lines (BAI2 / camt.053) against GL cash ledger postings, "
                "classifies matches and breaks, and alerts treasury operations.")
    oledb(p, "Treasury", "FinanceDW", "ETLControl")
    smtp(p)
    bd = p.parameter("BusinessDate", "DateTime", "2026-01-14T00:00:00", required=True)
    p.parameter("AmountTolerance", "Decimal", "0.01", description="Absolute tolerance for amount matching")
    audit_vars(p)
    p.variable("BreakCount", "Int32", "0")

    s = start_batch(p, "Bank statement reconciliation")
    clear = p.execute_sql("SQL Clear Recon Day", "Treasury",
                          "DELETE FROM trs.ReconMatched WHERE StatementDate = ?; DELETE FROM trs.ReconBreak WHERE StatementDate = ?; DELETE FROM trs.ReconSummary WHERE StatementDate = ?;",
                          params=[(bd, "Input", "DATE", "0"), (bd, "Input", "DATE", "1"), (bd, "Input", "DATE", "2")])
    dft, df = p.data_flow("DFT Match Bank To GL")
    bank = df.ole_src("OLE SRC Bank Statement Lines", "Treasury",
                      "SELECT BankAccountId, BankReference, StatementDate, ValueDate, Amount, BaiTypeCode\n"
                      "FROM trs.BankStatementLine WHERE StatementDate = ?",
                      [("BankAccountId", T.wstr(34)), ("BankReference", T.wstr(35)), ("StatementDate", T.DATE),
                       ("ValueDate", T.DATE), ("Amount", T.numeric(19, 2)), ("BaiTypeCode", T.wstr(3))],
                      params=[bd])
    gl = df.ole_src("OLE SRC GL Cash Postings", "FinanceDW",
                    "SELECT m.BankAccountId, f.ExternalReference AS BankReference, f.PostingDate, f.NetAmount AS Amount, f.JournalId\n"
                    "FROM dw.FactGLJournalLine f JOIN ref.GlBankAccountMap m ON m.GlAccount = f.GlAccount\n"
                    "WHERE f.PostingDate = ?",
                    [("BankAccountId", T.wstr(34)), ("BankReference", T.wstr(35)), ("PostingDate", T.DATE),
                     ("Amount", T.numeric(19, 2)), ("JournalId", T.I8)],
                    params=[bd])
    bank_sorted = df.sort("SRT Bank", bank, ["BankAccountId", "BankReference"])
    gl_sorted = df.sort("SRT GL", gl, ["BankAccountId", "BankReference"])
    joined = df.merge_join("MJ Full Outer Bank GL", bank_sorted, gl_sorted, "FullOuter", 2,
                           [("BankAccountId", "Bank_AccountId"), ("BankReference", "Bank_Reference"),
                            ("StatementDate", "Bank_StatementDate"), ("Amount", "Bank_Amount"),
                            ("BaiTypeCode", "Bank_BaiTypeCode")],
                           [("BankAccountId", "GL_AccountId"), ("BankReference", "GL_Reference"),
                            ("PostingDate", "GL_PostingDate"), ("Amount", "GL_Amount"), ("JournalId", "GL_JournalId")])
    der = df.derived("DER Classify", joined, [
        ("ReconAccountId", "ISNULL([Bank_AccountId]) ? [GL_AccountId] : [Bank_AccountId]", T.wstr(34)),
        ("StatementDate", "(DT_DBDATE)@[$Package::BusinessDate]", T.DATE),
        ("Variance", "(ISNULL([Bank_Amount]) ? (DT_NUMERIC,19,2)0 : [Bank_Amount]) - (ISNULL([GL_Amount]) ? (DT_NUMERIC,19,2)0 : [GL_Amount])", T.numeric(19, 2)),
        ("MatchStatus", 'ISNULL([GL_Reference]) ? "BANK_ONLY" : ISNULL([Bank_Reference]) ? "GL_ONLY" : ABS([Bank_Amount] - [GL_Amount]) > @[$Package::AmountTolerance] ? "AMOUNT_BREAK" : "MATCHED"', T.wstr(12)),
    ])
    split = df.cond_split("CSPL Match Status", der, [("Matched", '[MatchStatus] == "MATCHED"')],
                          default_name="Breaks")
    df.ole_dest("OLE DST ReconMatched", split["Matched"], "Treasury", "[trs].[ReconMatched]")
    mc = df.multicast("MC Breaks", split["Breaks"], 2)
    df.ole_dest("OLE DST ReconBreak", mc[0], "Treasury", "[trs].[ReconBreak]")
    summary = df.aggregate("AGG Break Summary", mc[1], [
        ("ReconAccountId", "GroupBy", "ReconAccountId", T.wstr(34)),
        ("StatementDate", "GroupBy", "StatementDate", T.DATE),
        ("MatchStatus", "GroupBy", "MatchStatus", T.wstr(12)),
        ("BreakCount", "CountAll", None, T.I8),
        ("TotalVariance", "Sum", "Variance", T.numeric(19, 2))])
    df.ole_dest("OLE DST ReconSummary", summary, "Treasury", "[trs].[ReconSummary]")

    count = p.execute_sql("SQL Count Breaks", "Treasury",
                          "SELECT COUNT(*) AS BreakCount FROM trs.ReconBreak WHERE StatementDate = ? AND ABS(Variance) > ?;",
                          result_type="SingleRow", results=[("BreakCount", "User::BreakCount")],
                          params=[(bd, "Input", "DATE", "0"), ("$Package::AmountTolerance", "Input", "DECIMAL", "1")])
    exc = p.execute_sql("SQL Create Exception Cases", "Treasury",
                        "EXEC trs.usp_CreateReconExceptionCases @StatementDate = ?, @AgingDays = 3;",
                        params=[(bd, "Input", "DATE", "0")])
    mail = p.send_mail("MAIL Treasury Breaks", "SMTP_Contoso", sender="treasury-recon@contoso.example",
                       to="treasury-ops@contoso.example", subject="Bank reconciliation breaks",
                       body="Unreconciled bank vs GL items exist. See trs.ReconBreak.")
    mail.expr("Subject", '"Bank rec: " + (DT_WSTR,10)@[User::BreakCount] + " breaks for " + (DT_WSTR,30)@[$Package::BusinessDate]')
    e = end_batch(p)
    p.chain(s, clear, dft, count)
    p.constraint(count, exc, expression="@[User::BreakCount] > 0", eval_op="ExpressionAndConstraint")
    p.chain(exc, mail)
    p.constraint(count, e, expression="@[User::BreakCount] == 0", eval_op="ExpressionAndConstraint",
                 logical_and=False)
    p.constraint(mail, e, logical_and=False)
    return p, {
        "domain": "Treasury / Cash management",
        "scenario": "Daily bank statement vs GL cash reconciliation",
        "regulatory_context": "SOX cash controls; intraday liquidity monitoring (BCBS 248)",
        "schedule": "Daily after prior-day BAI2/camt.053 statements are parsed",
        "features": ["Two sources from different connection managers", "Sort x2 + Merge Join (full outer, 2 keys)",
                     "Derived Column nested conditional with tolerance parameter", "Conditional Split", "Multicast",
                     "Aggregate summary", "Expression precedence + OR join", "Send Mail with expression subject"],
        "expected_adf": ["Mapping Data Flow with Join (full outer) - sorts unnecessary",
                         "Lookup activity + If Condition", "Logic App for email"],
        "edge_cases": ["Sort/Merge Join null semantics (TreatNullsAsEqual) differ from Spark join",
                       "Decimal(19,2) tolerance math must match exactly",
                       "Duplicate bank references -> many-to-many join explosion"],
        "complexity_hint": "High",
    }


# --------------------------------------------------------------------------- #
# 08 Credit risk exposure aggregation                                         #
# --------------------------------------------------------------------------- #
def credit_exposure() -> tuple[Package, dict]:
    p = Package("RISK_Credit_Exposure_Aggregation",
                "Aggregates commercial, mortgage and card exposures to counterparty level in USD for risk "
                "data aggregation (BCBS 239) and FR Y-14Q style quarterly schedules.")
    oledb(p, "CoreBanking", "CardsLegacy", "FinanceDW", "RiskMart", "ETLControl")
    bd = p.parameter("BusinessDate", "DateTime", "2026-01-14T00:00:00", required=True)
    p.parameter("CreditConversionFactor", "Decimal", "0.5", description="CCF applied to undrawn commitments")
    audit_vars(p)
    p.variable("LargeExposureCount", "Int32", "0")

    s = start_batch(p, "Credit exposure aggregation")
    trunc = p.execute_sql("SQL Truncate Exposure Stage", "RiskMart",
                          "TRUNCATE TABLE risk.ExposureStage; DELETE FROM risk.CounterpartyExposure WHERE AsOfDate = ?;",
                          params=[(bd, "Input", "DATE", "0")])
    dft, df = p.data_flow("DFT Aggregate Exposures")
    cols = [("CounterpartyId", T.wstr(20)), ("Portfolio", T.wstr(20)), ("FacilityId", T.wstr(30)),
            ("CurrencyCode", T.wstr(3)), ("OutstandingAmt", T.numeric(19, 4)), ("UndrawnAmt", T.numeric(19, 4)),
            ("Pd", T.numeric(9, 6)), ("Lgd", T.numeric(9, 6))]
    commercial = df.ole_src(
        "OLE SRC Commercial Loans (Linked Server)", "CoreBanking",
        "SELECT CounterpartyId, 'COMMERCIAL' AS Portfolio, FacilityId, CurrencyCode, OutstandingAmt, UndrawnAmt, Pd, Lgd\n"
        "FROM OPENQUERY([LOANSRV], 'SELECT cp_id AS CounterpartyId, facility_no AS FacilityId, ccy AS CurrencyCode,\n"
        "  outstanding AS OutstandingAmt, undrawn AS UndrawnAmt, pd_1y AS Pd, lgd_dt AS Lgd\n"
        "  FROM cl.facility_balance WHERE status <> ''CLOSED''')",
        cols)
    mortgage = df.ole_src(
        "OLE SRC Mortgages (Four-Part Name)", "CoreBanking",
        "SELECT b.BorrowerId AS CounterpartyId, 'MORTGAGE' AS Portfolio, l.LoanNumber AS FacilityId, 'USD' AS CurrencyCode,\n"
        "       l.UnpaidPrincipal AS OutstandingAmt, CAST(0 AS decimal(19,4)) AS UndrawnAmt, r.Pd, r.Lgd\n"
        "FROM [MORTGAGESRV].[Mortgage].[dbo].[LoanBalance] l\n"
        "JOIN [MORTGAGESRV].[Mortgage].[dbo].[Borrower] b ON b.LoanNumber = l.LoanNumber AND b.IsPrimary = 1\n"
        "JOIN RiskMart.risk.RetailPdLgd r ON r.ProductCode = l.ProductCode AND r.Bucket = l.DelinquencyBucket\n"
        "WHERE l.AsOfDate = ?",
        cols, params=[bd])
    cards = df.ole_src(
        "OLE SRC Card Accounts (Cross-DB)", "CardsLegacy",
        "SELECT a.CustomerId AS CounterpartyId, 'CARD' AS Portfolio, a.CardAccountId AS FacilityId, a.CurrencyCode,\n"
        "       a.CurrentBalance AS OutstandingAmt, a.CreditLimit - a.CurrentBalance AS UndrawnAmt, s.Pd, s.Lgd\n"
        "FROM CardPlatform.dbo.Account a\n"
        "JOIN CardScoring.dbo.AccountScore s ON s.CardAccountId = a.CardAccountId AND s.ScoreDate = ?\n"
        "WHERE a.Status IN ('OPEN','DELINQ')",
        cols, params=[bd])
    union = df.union_all("UALL Portfolios", [commercial, mortgage, cards], cols)
    match, no_fx = df.lookup("LKP FX To USD", union, "FinanceDW",
                             "SELECT CurrencyCode, UnitsPerUsd FROM ref.FxRate WHERE RateDate = (SELECT MAX(RateDate) FROM ref.FxRate)",
                             joins=[("CurrencyCode", "CurrencyCode")],
                             copy=[("UnitsPerUsd", "UnitsPerUsd", T.numeric(19, 8))])
    ead = df.derived("DER EAD USD", match, [
        ("EadUsd", "([OutstandingAmt] + @[$Package::CreditConversionFactor] * ([UndrawnAmt] < 0 ? 0 : [UndrawnAmt])) / [UnitsPerUsd]", T.numeric(19, 4))])
    el = df.derived("DER Expected Loss", ead, [
        ("ExpectedLossUsd", "[EadUsd] * [Pd] * [Lgd]", T.numeric(19, 4)),
        ("AsOfDate", "(DT_DBDATE)@[$Package::BusinessDate]", T.DATE)])
    mc = df.multicast("MC Facility And Counterparty", el, 2)
    df.ole_dest("OLE DST ExposureStage", mc[0], "RiskMart", "[risk].[ExposureStage]")
    agg = df.aggregate("AGG Counterparty Exposure", mc[1], [
        ("CounterpartyId", "GroupBy", "CounterpartyId", T.wstr(20)),
        ("Portfolio", "GroupBy", "Portfolio", T.wstr(20)),
        ("AsOfDate", "GroupBy", "AsOfDate", T.DATE),
        ("FacilityCount", "CountAll", None, T.I8),
        ("TotalEadUsd", "Sum", "EadUsd", T.numeric(19, 4)),
        ("TotalExpectedLossUsd", "Sum", "ExpectedLossUsd", T.numeric(19, 4)),
        ("MaxPd", "Max", "Pd", T.numeric(9, 6))])
    rc = df.row_count("RC Counterparties", agg, "User::RowsLoaded")
    df.ole_dest("OLE DST CounterpartyExposure", rc, "RiskMart", "[risk].[CounterpartyExposure]")
    df.ole_dest("OLE DST Missing FX", no_fx, "RiskMart", "[risk].[ExposureMissingFx]")

    large = p.execute_sql(
        "SQL Large Exposure Check", "RiskMart",
        "SELECT COUNT(*) AS LargeExposureCount FROM risk.CounterpartyExposure ce\n"
        "JOIN [RISKSQL01].[RiskMart].[ref].[Tier1Capital] t ON t.AsOfDate = EOMONTH(ce.AsOfDate, -1)\n"
        "WHERE ce.AsOfDate = ? GROUP BY ce.CounterpartyId, t.Tier1CapitalUsd HAVING SUM(ce.TotalEadUsd) > 0.10 * t.Tier1CapitalUsd;",
        result_type="SingleRow", results=[("LargeExposureCount", "User::LargeExposureCount")],
        params=[(bd, "Input", "DATE", "0")])
    y14 = p.execute_sql(
        "SQL Publish FR Y-14Q Extract", "RiskMart",
        "INSERT INTO [REGRPTSQL01].[RegReporting].[y14q].[CorporateLoanStage] (AsOfDate, ObligorId, FacilityId, CommittedExposureUsd, UtilizedExposureUsd, Pd, Lgd)\n"
        "SELECT s.AsOfDate, s.CounterpartyId, s.FacilityId, (s.OutstandingAmt + s.UndrawnAmt) / f.UnitsPerUsd, s.OutstandingAmt / f.UnitsPerUsd, s.Pd, s.Lgd\n"
        "FROM risk.ExposureStage s JOIN FinanceDW.ref.FxRate f ON f.CurrencyCode = s.CurrencyCode AND f.RateDate = s.AsOfDate\n"
        "WHERE s.Portfolio = 'COMMERCIAL' AND (s.OutstandingAmt + s.UndrawnAmt) >= 1000000;",
        description="Cross-server INSERT via linked server to regulatory reporting staging")
    e = end_batch(p)
    p.chain(s, trunc, dft, large, y14, e)
    return p, {
        "domain": "Risk / Credit risk",
        "scenario": "Multi-portfolio exposure aggregation to counterparty in USD",
        "regulatory_context": "BCBS 239 risk data aggregation; FR Y-14Q corporate loan schedule; large exposure limits",
        "schedule": "Daily; quarter-end run feeds FR Y-14Q",
        "features": ["Three OLE DB sources across two connection managers", "OPENQUERY linked server",
                     "Four-part and three-part (cross-database) names", "Union All", "Lookup (FX)",
                     "Chained Derived Columns using parameters", "Multicast", "Aggregate (Sum/Max/CountAll)",
                     "Cross-server INSERT in Execute SQL"],
        "expected_adf": ["Mapping Data Flow or ELT in Azure SQL/Fabric Warehouse",
                         "Linked servers replaced by separate linked services / external tables"],
        "edge_cases": ["Linked servers and cross-DB joins are not supported in Azure SQL Database",
                       "FX lookup uses MAX(RateDate) not business date (latent bug to surface)",
                       "Lookup no-match rows go to side table -> totals reconciliation must include them"],
        "complexity_hint": "High",
    }


# --------------------------------------------------------------------------- #
# 09 OFAC sanctions screening                                                 #
# --------------------------------------------------------------------------- #
OFAC_SCRIPT = r'''#region Namespaces
using System;
using System.IO;
using System.Net;
using Microsoft.SqlServer.Dts.Runtime;
#endregion

namespace ST_DownloadSdn
{
    [Microsoft.SqlServer.Dts.Tasks.ScriptTask.SSISScriptTaskEntryPointAttribute]
    public partial class ScriptMain : Microsoft.SqlServer.Dts.Tasks.ScriptTask.VSTARTScriptObjectModelBase
    {
        // Downloads the OFAC SDN list (CSV) through the corporate proxy and records its SHA-256
        // so compliance can evidence which list version was used for screening.
        public void Main()
        {
            string url = (string)Dts.Variables["$Package::SdnListUrl"].Value;
            string target = (string)Dts.Variables["User::SdnFilePath"].Value;
            try
            {
                ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
                using (var client = new WebClient())
                {
                    client.Proxy = new WebProxy("http://proxy.contoso.local:8080", true) { UseDefaultCredentials = true };
                    client.DownloadFile(url, target);
                }
                using (var sha = System.Security.Cryptography.SHA256.Create())
                using (var fs = File.OpenRead(target))
                {
                    Dts.Variables["User::SdnFileHash"].Value = BitConverter.ToString(sha.ComputeHash(fs)).Replace("-", "");
                }
                if (new FileInfo(target).Length < 1024 * 100)
                {
                    Dts.Events.FireError(0, "SDN Download", "SDN file unexpectedly small - aborting screening", "", 0);
                    Dts.TaskResult = (int)ScriptResults.Failure;
                    return;
                }
                Dts.TaskResult = (int)ScriptResults.Success;
            }
            catch (WebException ex)
            {
                Dts.Events.FireError(0, "SDN Download", ex.Message, "", 0);
                Dts.TaskResult = (int)ScriptResults.Failure;
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


def ofac_screening() -> tuple[Package, dict]:
    p = Package("CMP_OFAC_Sanctions_Screening",
                "Downloads the OFAC SDN list, loads it, and fuzzy-matches customer names against it to "
                "create sanctions alerts and a review queue.")
    oledb(p, "CRM", "Compliance", "ETLControl")
    smtp(p)
    p.parameter("SdnListUrl", "String", "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV",
                required=True, description="OFAC Sanctions List Service SDN CSV export")
    p.parameter("MinSimilarity", "Decimal", "0.80")
    audit_vars(p)
    p.variable("SdnFilePath", "String", "",
               expression=r'@[$Project::InboundRoot] + "\\ofac\\SDN_" + ' + yyyymmdd("GETDATE()") + ' + ".csv"')
    p.variable("SdnFileHash", "String", "")
    p.variable("AlertCount", "Int32", "0")
    p.flat_file("FF_SdnList", r"\\fileshare01.contoso.local\etl\inbound\ofac\SDN.csv",
                [("EntNum", 10, STR), ("SdnName", 350, STR), ("SdnType", 12, STR), ("Program", 200, STR),
                 ("Title", 200, STR), ("CallSign", 8, STR), ("VesselType", 25, STR), ("Tonnage", 14, STR),
                 ("Grt", 8, STR), ("VesselFlag", 40, STR), ("VesselOwner", 150, STR), ("Remarks", 1000, STR)],
                expression="@[User::SdnFilePath]", header=False, code_page=1252)

    s = start_batch(p, "OFAC screening")
    dl = p.script_task("SCR Download SDN List", OFAC_SCRIPT,
                       read_only="$Package::SdnListUrl,User::SdnFilePath", read_write="User::SdnFileHash",
                       description="C#: HTTPS download via proxy + SHA-256 evidence")
    trunc = p.execute_sql("SQL Truncate SDN", "Compliance",
                          "TRUNCATE TABLE cmp.SdnEntry; INSERT INTO cmp.SdnListVersion (BatchId, FileHash, LoadedUtc) VALUES (?, ?, SYSUTCDATETIME());",
                          params=[("User::BatchId", "Input", "BIGINT", "0"), ("User::SdnFileHash", "Input", "NVARCHAR", "1")])
    load, ldf = p.data_flow("DFT Load SDN List")
    sdn = ldf.flat_src("FF Source SDN", "FF_SdnList",
                       [("EntNum", T.str(10)), ("SdnName", T.str(350)), ("SdnType", T.str(12)),
                        ("Program", T.str(200)), ("Title", T.str(200)), ("CallSign", T.str(8)),
                        ("VesselType", T.str(25)), ("Tonnage", T.str(14)), ("Grt", T.str(8)),
                        ("VesselFlag", T.str(40)), ("VesselOwner", T.str(150)), ("Remarks", T.str(1000))])
    sdn_norm = ldf.derived("DER Normalize SDN", sdn, [
        ("NormalizedName", 'UPPER(TRIM(REPLACE(REPLACE([SdnName], ",", " "), ".", "")))', T.wstr(350)),
        ("IsNullPlaceholder", '[SdnType] == "-0- "', T.BOOL)])
    ldf.ole_dest("OLE DST SdnEntry", sdn_norm, "Compliance", "[cmp].[SdnEntry]")

    screen, sdf = p.data_flow("DFT Screen Customers")
    cust = sdf.ole_src("OLE SRC Active Customers", "CRM",
                       "SELECT CustomerNumber, FirstName, LastName, CountryOfResidence, DateOfBirth FROM crm.Customer WHERE Status = 'ACTIVE'",
                       [("CustomerNumber", T.wstr(20)), ("FirstName", T.wstr(50)), ("LastName", T.wstr(80)),
                        ("CountryOfResidence", T.wstr(2)), ("DateOfBirth", T.DATE)])
    cust_norm = sdf.derived("DER Normalize Customer", cust, [
        ("NormalizedName", 'UPPER(TRIM([LastName]) + " " + TRIM([FirstName]))', T.wstr(350))])
    fuzzy = sdf.fuzzy_lookup("FZL SDN Match", cust_norm, "Compliance", "[cmp].[SdnEntry]",
                             ("NormalizedName", "NormalizedName"),
                             [("EntNum", "SdnEntNum", T.str(10)), ("SdnName", "SdnName", T.str(350)),
                              ("Program", "SdnProgram", T.str(200))], min_similarity=0.80)
    split = sdf.cond_split("CSPL Match Strength", fuzzy, [
        ("Strong Match", "[_Similarity] >= 0.92 && [_Confidence] >= 0.5"),
        ("Possible Match", "[_Similarity] >= (DT_R4)@[$Package::MinSimilarity]"),
    ], default_name="No Match")
    alert = sdf.derived("DER Alert Attributes", split["Strong Match"], [
        ("BatchId", "@[User::BatchId]", T.I8), ("AlertStatus", '"OPEN"', T.wstr(10))])
    rc = sdf.row_count("RC Alerts", alert, "User::AlertCount")
    sdf.ole_dest("OLE DST SanctionsAlert", rc, "Compliance", "[cmp].[SanctionsAlert]")
    review = sdf.derived("DER Review Attributes", split["Possible Match"], [("BatchId", "@[User::BatchId]", T.I8)])
    sdf.ole_dest("OLE DST SanctionsReviewQueue", review, "Compliance", "[cmp].[SanctionsReviewQueue]")

    mail = p.send_mail("MAIL Sanctions Alert", "SMTP_Contoso", sender="sanctions@contoso.example",
                       to="sanctions-team@contoso.example", subject="OFAC potential matches",
                       body="Strong potential OFAC SDN matches were found. Review cmp.SanctionsAlert immediately.",
                       priority="High")
    mail.expr("Subject", '"[OFAC] " + (DT_WSTR,10)@[User::AlertCount] + " strong potential matches"')
    e = end_batch(p)
    p.chain(s, dl, trunc, load, screen)
    p.constraint(screen, mail, expression="@[User::AlertCount] > 0", eval_op="ExpressionAndConstraint")
    p.constraint(screen, e)
    on_error_logging(p)
    return p, {
        "domain": "Compliance / Sanctions",
        "scenario": "OFAC SDN download and fuzzy customer name screening",
        "regulatory_context": "OFAC sanctions compliance (31 CFR Chapter V); evidence of list version used",
        "schedule": "Daily, plus on-demand when OFAC publishes updates",
        "features": ["Script Task HTTP download via proxy with hash evidence",
                     "Headerless flat file with 12 columns", "Fuzzy Lookup (Enterprise edition)",
                     "Conditional Split on _Similarity/_Confidence", "Row Count driving email",
                     "OnError event handler"],
        "expected_adf": ["Web/Copy activity (HTTP linked service) for download",
                         "No native fuzzy lookup: Azure AI Search / Spark notebook / SQL similarity function",
                         "Logic App for alert email"],
        "edge_cases": ["Fuzzy Lookup has no ADF equivalent and match scores will differ after migration",
                       "False-negative risk: reconciliation must compare alert sets, not counts",
                       "Corporate proxy and hash evidence must be preserved"],
        "complexity_hint": "Very High",
    }


# --------------------------------------------------------------------------- #
# 10 EOD orchestration master                                                 #
# --------------------------------------------------------------------------- #
def eod_master() -> tuple[Package, dict]:
    p = Package("ORCH_EOD_Regulatory_Batch_Master",
                "Master end-of-day orchestration: waits for core banking EOD, then runs reference data, "
                "finance, payments and regulatory child packages with dependency and failure handling.")
    oledb(p, "CoreBanking", "ETLControl")
    smtp(p)
    bd = p.parameter("BusinessDate", "DateTime", "2026-01-14T00:00:00", required=True)
    audit_vars(p)
    p.variable("EodComplete", "Boolean", "False")
    p.variable("PollAttempt", "Int32", "0")
    p.variable("MaxPollAttempts", "Int32", "90")

    s = start_batch(p, "EOD master")
    loop = p.for_loop("FLC Wait For Core EOD", init="@[User::PollAttempt] = 0",
                      eval_="@[User::EodComplete] == False && @[User::PollAttempt] < @[User::MaxPollAttempts]",
                      assign="@[User::PollAttempt] = @[User::PollAttempt] + 1")
    check = loop.execute_sql("SQL Check EOD Flag", "CoreBanking",
                             "SELECT CAST(CASE WHEN EXISTS (SELECT 1 FROM ops.EodStatus WHERE BusinessDate = ? AND Status = 'COMPLETE') THEN 1 ELSE 0 END AS bit) AS EodComplete;",
                             result_type="SingleRow", results=[("EodComplete", "User::EodComplete")],
                             params=[(bd, "Input", "DATE", "0")])
    wait = loop.execute_sql("SQL Wait 60 Seconds", "ETLControl", "WAITFOR DELAY '00:01:00';")
    loop.constraint(check, wait, expression="@[User::EodComplete] == False", eval_op="ExpressionAndConstraint")

    timeout_mail = p.send_mail("MAIL EOD Timeout", "SMTP_Contoso", sender="eod-batch@contoso.example",
                               to="etl-ops@contoso.example", subject="Core banking EOD not complete",
                               body="Core EOD flag not set after maximum polling attempts. Regulatory batch NOT started.",
                               priority="High")
    timeout_fail = p.execute_sql("SQL Fail Batch Timeout", "ETLControl",
                                 "EXEC etl.usp_EndBatch @BatchId = ?, @Status = 'TimedOut'; THROW 50010, 'Core EOD wait timed out', 1;",
                                 params=[("User::BatchId", "Input", "BIGINT", "0")])

    pass_bd = [("BusinessDate", "$Package::BusinessDate")]
    ref = p.sequence("SEQ Reference Data")
    fx = ref.execute_package("EPT FX Rates", "FIN_FX_DailyRates_Load.dtsx", pass_bd)
    ref.execute_package("EPT Custodian Positions", "OPS_Custodian_Positions_Ingest.dtsx")
    _ = fx

    fin = p.sequence("SEQ Core Finance")
    gl = fin.execute_package("EPT GL Journals", "FIN_GL_JournalEntries_Incremental.dtsx")
    kyc = fin.execute_package("EPT Customer KYC", "CRM_Customer_KYC_SCD2.dtsx", pass_bd)
    _ = (gl, kyc)

    pay = p.execute_package("EPT ACH Outbound", "PAY_ACH_NACHA_Outbound.dtsx")

    reg = p.sequence("SEQ Regulatory And Compliance")
    aml = reg.execute_package("EPT AML CTR", "AML_CTR_Daily_Monitoring.dtsx", pass_bd)
    ofac = reg.execute_package("EPT OFAC Screening", "CMP_OFAC_Sanctions_Screening.dtsx")
    rec = reg.execute_package("EPT Bank Reconciliation", "TRS_Bank_Statement_Reconciliation.dtsx", pass_bd)
    risk = reg.execute_package("EPT Credit Exposure", "RISK_Credit_Exposure_Aggregation.dtsx", pass_bd)
    reg.constraint(rec, risk, "Completion")
    _ = (aml, ofac)

    close = end_batch(p, "SQL Close Batch")
    fail = p.execute_sql("SQL Mark Batch Failed", "ETLControl",
                         "EXEC etl.usp_EndBatch @BatchId = ?, @Status = 'Failed';",
                         params=[("User::BatchId", "Input", "BIGINT", "0")])

    p.chain(s, loop)
    p.constraint(loop, ref, expression="@[User::EodComplete] == True", eval_op="ExpressionAndConstraint")
    p.constraint(loop, timeout_mail, expression="@[User::EodComplete] == False", eval_op="ExpressionAndConstraint")
    p.chain(timeout_mail, timeout_fail)
    p.constraint(ref, fin)
    p.constraint(ref, pay)
    p.constraint(fin, reg)
    p.constraint(reg, close)
    p.constraint(pay, close, "Completion")
    p.constraint(fin, fail, "Failure", logical_and=False)
    p.constraint(reg, fail, "Failure", logical_and=False)

    handler = p.event_handler("OnError")
    mail = handler.send_mail("MAIL EOD Failure", "SMTP_Contoso", sender="eod-batch@contoso.example",
                             to="etl-ops@contoso.example", subject="EOD regulatory batch failure",
                             body="A task in the EOD master failed.", priority="High")
    mail.expr("MessageSource", '"Task " + @[System::SourceName] + " failed: " + @[System::ErrorDescription]')
    return p, {
        "domain": "Orchestration",
        "scenario": "End-of-day master orchestration across finance, payments, risk and compliance",
        "regulatory_context": "Timely regulatory reporting SLAs; BCBS 239 timeliness and completeness",
        "schedule": "SQL Agent job, weeknights 21:00 ET (Sprint 2 adds the job definition)",
        "features": ["For Loop polling with WAITFOR", "Execute Package Task (project reference) x9",
                     "Parameter assignments to child packages", "Parallel tasks inside Sequence containers",
                     "Expression constraints for success/timeout branches", "Completion constraints",
                     "OR (LogicalAnd=False) failure fan-in", "OnError event handler email"],
        "expected_adf": ["Until activity with Wait", "Execute Pipeline activities (waitOnCompletion)",
                         "Parallel branches via dependsOn", "Pipeline parameters passed to children"],
        "edge_cases": ["For Loop EvalExpression must be negated for ADF Until",
                       "Completion constraint means payments failure does not block close",
                       "Sequence-level Failure constraints fire if any child fails",
                       "Child packages must be converted first (dependency order)"],
        "complexity_hint": "Medium",
    }
