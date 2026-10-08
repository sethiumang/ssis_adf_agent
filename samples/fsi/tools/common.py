"""Shared Contoso Bank connection managers, project parameters and helpers."""
from __future__ import annotations

from dtsx_builder import Package

PROJECT_NAME = "ContosoBank.EOD"

# (name, dtype, value, required, sensitive, description)
PROJECT_PARAMS = [
    ("Environment", "String", "DEV", True, False, "DEV | UAT | PROD"),
    ("CoreBankingConnStr", "String",
     "Data Source=CORESQL01.contoso.local;Initial Catalog=CoreBanking;Provider=MSOLEDBSQL.1;Integrated Security=SSPI;",
     True, False, "Core banking OLTP (read-only replica)"),
    ("FinanceDWConnStr", "String",
     "Data Source=DWSQL01.contoso.local;Initial Catalog=FinanceDW;Provider=MSOLEDBSQL.1;Integrated Security=SSPI;",
     True, False, "Enterprise finance data warehouse"),
    ("ETLControlConnStr", "String",
     "Data Source=ETLSQL01.contoso.local;Initial Catalog=ETLControl;Provider=MSOLEDBSQL.1;Integrated Security=SSPI;",
     True, False, "Batch control, watermarks and audit"),
    ("InboundRoot", "String", r"\\fileshare01.contoso.local\etl\inbound", True, False, "Inbound landing share"),
    ("ArchiveRoot", "String", r"\\fileshare01.contoso.local\etl\archive", True, False, "Archive share"),
    ("OutboundRoot", "String", r"\\fileshare01.contoso.local\etl\outbound", True, False, "Outbound staging share"),
    ("SftpHost", "String", "sftp.fedach-gateway.example", True, False, "Bank ACH operator SFTP endpoint (fictional)"),
    ("SftpUser", "String", "contoso_ach", True, False, "SFTP user"),
    ("SftpPassword", "String", "AQAAANCMnd8BFdERjHoAwE/Cl+sBAAAA-FAKE-ENCRYPTED-VALUE", True, True,
     "SFTP password (sensitive; fake encrypted blob)"),
    ("PgpRecipient", "String", "ach-operator@fedach-gateway.example", True, False, "PGP key id for outbound files"),
    ("BsaOfficerEmail", "String", "bsa.officer@contoso.example", True, False, "BSA/AML officer distribution list"),
    ("OpsAlertEmail", "String", "etl-ops@contoso.example", True, False, "Operations alert distribution list"),
    ("CtrThresholdUsd", "Decimal", "10000", True, False, "31 CFR 1010.311 CTR threshold"),
]

SERVERS = {
    "CoreBanking": ("CORESQL01.contoso.local", "CoreBanking", "CoreBankingConnStr"),
    "FinanceDW": ("DWSQL01.contoso.local", "FinanceDW", "FinanceDWConnStr"),
    "ETLControl": ("ETLSQL01.contoso.local", "ETLControl", "ETLControlConnStr"),
    "CRM": ("CRMSQL01.contoso.local", "ContosoCRM", None),
    "InvestmentOps": ("INVSQL01.contoso.local", "InvestmentOps", None),
    "Treasury": ("TRSQL01.contoso.local", "Treasury", None),
    "RiskMart": ("RISKSQL01.contoso.local", "RiskMart", None),
    "Compliance": ("CMPSQL01.contoso.local", "Compliance", None),
    "CardsLegacy": ("CARDSQL2012.contoso.local", "CardPlatform", None),
}


def yyyymmdd(expr: str) -> str:
    """SSIS expression that renders a DateTime as yyyyMMdd."""
    return (f'(DT_WSTR,4)YEAR({expr}) + RIGHT("0" + (DT_WSTR,2)MONTH({expr}),2) + '
            f'RIGHT("0" + (DT_WSTR,2)DAY({expr}),2)')


def oledb(pkg: Package, *names: str) -> None:
    for name in names:
        server, db, param = SERVERS[name]
        pkg.oledb(name, server, db, project_param=param)


def smtp(pkg: Package) -> str:
    return pkg.smtp("SMTP_Contoso", "smtp.contoso.local")


def audit_vars(pkg: Package) -> None:
    pkg.variable("BatchId", "Int64", "0", description="ETLControl batch id")
    pkg.variable("RowsExtracted", "Int32", "0")
    pkg.variable("RowsLoaded", "Int32", "0")
    pkg.variable("RowsRejected", "Int32", "0")


def start_batch(pkg: Package, process: str):
    return pkg.execute_sql(
        "SQL Start Batch", "ETLControl",
        "EXEC etl.usp_StartBatch @ProcessName = ?, @PackageName = ?, @ExecutionId = ?, @BatchId = ? OUTPUT;",
        params=[("System::PackageName", "Input", "NVARCHAR", "0"),
                ("System::PackageName", "Input", "NVARCHAR", "1"),
                ("System::ServerExecutionID", "Input", "BIGINT", "2"),
                ("User::BatchId", "Output", "BIGINT", "3")],
        description=f"Register {process} run in ETLControl.etl.Batch")


def end_batch(pkg: Package, name: str = "SQL Complete Batch", status: str = "Succeeded"):
    return pkg.execute_sql(
        name, "ETLControl",
        f"EXEC etl.usp_EndBatch @BatchId = ?, @Status = '{status}', @RowsExtracted = ?, @RowsLoaded = ?, @RowsRejected = ?;",
        params=[("User::BatchId", "Input", "BIGINT", "0"),
                ("User::RowsExtracted", "Input", "LONG", "1"),
                ("User::RowsLoaded", "Input", "LONG", "2"),
                ("User::RowsRejected", "Input", "LONG", "3")],
        description=f"Close batch with status {status}")


def on_error_logging(pkg: Package) -> None:
    handler = pkg.event_handler("OnError")
    handler.execute_sql(
        "SQL Log Error", "ETLControl",
        "INSERT INTO etl.ErrorLog (BatchId, PackageName, SourceName, ErrorCode, ErrorDescription, LoggedAtUtc) "
        "VALUES (?, ?, ?, ?, ?, SYSUTCDATETIME());",
        params=[("User::BatchId", "Input", "BIGINT", "0"),
                ("System::PackageName", "Input", "NVARCHAR", "1"),
                ("System::SourceName", "Input", "NVARCHAR", "2"),
                ("System::ErrorCode", "Input", "LONG", "3"),
                ("System::ErrorDescription", "Input", "NVARCHAR", "4")],
        description="Write failing task and error text to ETLControl.etl.ErrorLog")
