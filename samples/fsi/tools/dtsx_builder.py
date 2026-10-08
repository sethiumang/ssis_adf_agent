"""Deterministic builder for SSIS 2012+ (PackageFormatVersion 8) .dtsx files.

The goal is a realistic, reproducible test corpus — not a replacement for SSDT.
Structure follows the XML that SSDT/Visual Studio writes for SQL Server 2019
project-deployment-model packages:

* control flow lives in the ``www.microsoft.com/SqlServer/Dts`` namespace
* executables, constraints and data-flow objects are addressed by path-style
  ``refId`` values (``Package\\Seq\\Task``), not GUIDs
* data-flow (``Microsoft.Pipeline``) XML is un-namespaced and wires columns by
  lineage ``refId``; expressions reference columns as ``#{lineageRefId}``

All GUIDs are UUIDv5 values derived from package name + refId so regenerating
produces byte-identical output.
"""
from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

DTS_NS = "www.microsoft.com/SqlServer/Dts"
SQLTASK_NS = "www.microsoft.com/sqlserver/dts/tasks/sqltask"
SENDMAIL_NS = "www.microsoft.com/sqlserver/dts/tasks/sendmailtask"
SSIS_NS = "www.microsoft.com/SqlServer/SSIS"
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_ENC_NS = "http://schemas.xmlsoap.org/soap/encoding/"
XSD_NS = "http://www.w3.org/2001/XMLSchema"

for _prefix, _uri in {
    "DTS": DTS_NS,
    "SQLTask": SQLTASK_NS,
    "SendMailTask": SENDMAIL_NS,
    "SSIS": SSIS_NS,
    "SOAP-ENV": SOAP_ENV_NS,
    "SOAP-ENC": SOAP_ENC_NS,
    "xsd": XSD_NS,
}.items():
    ET.register_namespace(_prefix, _uri)

GUID_NAMESPACE = uuid.UUID("6c1f2c4e-0d3a-4b8e-9f51-2b8a3c7d9e10")
CREATION_DATE = "1/15/2026 9:00:00 AM"
PRODUCT_VERSION = "15.0.2000.180"  # SQL Server 2019 SSDT
CREATOR_COMPUTER = "CONTOSO-ETLDEV01"
CREATOR_NAME = "CONTOSO\\svc_ssis_dev"


def d(name: str) -> str:
    return f"{{{DTS_NS}}}{name}"


def sq(name: str) -> str:
    return f"{{{SQLTASK_NS}}}{name}"


def sm(name: str) -> str:
    return f"{{{SENDMAIL_NS}}}{name}"


def guid(*parts: str) -> str:
    return "{" + str(uuid.uuid5(GUID_NAMESPACE, "|".join(parts))).upper() + "}"


# --------------------------------------------------------------------------- #
# Type codes                                                                  #
# --------------------------------------------------------------------------- #

# DTS:Variable / DTS:VariableValue uses VARTYPE codes.
VARIABLE_TYPES = {
    "String": "8",
    "Int32": "3",
    "Int64": "20",
    "DateTime": "7",
    "Boolean": "11",
    "Object": "13",
    "Decimal": "14",
    "Double": "5",
}

# DTS:PackageParameter / project parameters use System.TypeCode values.
PARAMETER_TYPES = {
    "Boolean": "3",
    "Int32": "9",
    "Int64": "11",
    "Decimal": "15",
    "DateTime": "16",
    "String": "18",
}

# Execute SQL Task parameter bindings use OLE DB DBTYPE codes.
OLEDB_PARAM_TYPES = {
    "LONG": "3",
    "DATE": "7",
    "DECIMAL": "131",
    "VARCHAR": "129",
    "NVARCHAR": "130",
    "DBTIMESTAMP": "135",
    "BIGINT": "20",
    "BOOL": "11",
}

PRECEDENCE_VALUES = {"Success": None, "Failure": "1", "Completion": "2"}
EVAL_OPS = {"Expression": "1", "Constraint": "2", "ExpressionAndConstraint": "3", "ExpressionOrConstraint": "4"}


# --------------------------------------------------------------------------- #
# Data-flow column types                                                      #
# --------------------------------------------------------------------------- #

class T:
    """Factory for pipeline column type attributes."""

    @staticmethod
    def wstr(length: int) -> dict[str, str]:
        return {"dataType": "wstr", "length": str(length)}

    @staticmethod
    def str(length: int, code_page: int = 1252) -> dict[str, str]:
        return {"dataType": "str", "length": str(length), "codePage": str(code_page)}

    @staticmethod
    def numeric(precision: int, scale: int) -> dict[str, str]:
        return {"dataType": "numeric", "precision": str(precision), "scale": str(scale)}

    I4 = {"dataType": "i4"}
    I8 = {"dataType": "i8"}
    BOOL = {"dataType": "bool"}
    DATE = {"dataType": "dbDate"}
    TIMESTAMP = {"dataType": "dbTimeStamp"}
    CY = {"dataType": "cy"}
    R4 = {"dataType": "r4"}
    R8 = {"dataType": "r8"}
    GUID = {"dataType": "guid"}


def _cached(attrs: dict[str, str]) -> dict[str, str]:
    mapping = {"dataType": "cachedDataType", "length": "cachedLength", "precision": "cachedPrecision",
               "scale": "cachedScale", "codePage": "cachedCodepage"}
    return {mapping[k]: v for k, v in attrs.items()}


def _props(parent: ET.Element, props: Sequence[tuple]) -> None:
    """Write a ``<properties>`` block. Each prop: (name, value[, dataType[, extra attrs]])."""
    if not props:
        return
    holder = ET.SubElement(parent, "properties")
    for prop in props:
        name, value = prop[0], prop[1]
        dtype = prop[2] if len(prop) > 2 and prop[2] else "System.String"
        extra = prop[3] if len(prop) > 3 else {}
        el = ET.SubElement(holder, "property", {"dataType": dtype, **extra, "name": name})
        if isinstance(value, list):
            el.set("isArray", "true")
            arr = ET.SubElement(el, "arrayElements", {"arrayElementCount": str(len(value))})
            for item in value:
                ET.SubElement(arr, "arrayElement", {"dataType": "System.String"}).text = item
        elif value is not None:
            el.text = str(value)


# --------------------------------------------------------------------------- #
# Data-flow model                                                             #
# --------------------------------------------------------------------------- #

@dataclass
class Col:
    name: str
    lineage: str
    attrs: dict[str, str]


@dataclass
class Output:
    comp: "Component"
    name: str
    sync_input: "Input | None" = None
    is_error: bool = False
    exclusion_group: int | None = None
    is_sorted: bool = False
    props: list[tuple] = field(default_factory=list)
    columns: list[tuple[Col, list[tuple], dict[str, str]]] = field(default_factory=list)
    external: list[Col] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.comp.ref}.Outputs[{self.name}]"

    def add(self, name: str, attrs: dict[str, str], props: Sequence[tuple] = (),
            extra: dict[str, str] | None = None, external: bool = False) -> Col:
        col = Col(name, f"{self.ref}.Columns[{name}]", attrs)
        ext_attrs = dict(extra or {})
        if external:
            ext = Col(name, f"{self.ref}.ExternalColumns[{name}]", attrs)
            self.external.append(ext)
            ext_attrs["externalMetadataColumnId"] = ext.lineage
        self.columns.append((col, list(props), ext_attrs))
        return col

    def available(self) -> list[Col]:
        base = self.sync_input.available() if self.sync_input is not None else []
        own = [c for c, _, _ in self.columns]
        names = {c.name for c in own}
        return [c for c in base if c.name not in names] + own

    def col(self, name: str) -> Col:
        for c in self.available():
            if c.name == name:
                return c
        raise KeyError(f"Column {name!r} not available on {self.ref}")


@dataclass
class Input:
    comp: "Component"
    name: str
    upstream: Output | None = None
    has_side_effects: bool = False
    columns: list[tuple[Col, Col, list[tuple], dict[str, str]]] = field(default_factory=list)
    external: list[Col] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.comp.ref}.Inputs[{self.name}]"

    def available(self) -> list[Col]:
        return self.upstream.available() if self.upstream is not None else []

    def use(self, upstream_col: Col, props: Sequence[tuple] = (), usage: str | None = None,
            external_name: str | None = None) -> Col:
        for _, src, _, _ in self.columns:
            if src.lineage == upstream_col.lineage:
                return src
        own = Col(upstream_col.name, f"{self.ref}.Columns[{upstream_col.name}]", upstream_col.attrs)
        extra: dict[str, str] = {}
        if usage:
            extra["usageType"] = usage
        if external_name:
            ext = Col(external_name, f"{self.ref}.ExternalColumns[{external_name}]", upstream_col.attrs)
            if all(e.name != external_name for e in self.external):
                self.external.append(ext)
            extra["externalMetadataColumnId"] = ext.lineage
        self.columns.append((own, upstream_col, list(props), extra))
        return own


@dataclass
class Component:
    flow: "DataFlow"
    name: str
    class_id: str
    description: str
    version: int = 1
    props: list[tuple] = field(default_factory=list)
    connection: tuple[str, str] | None = None  # (connection name, CM object name)
    inputs: list[Input] = field(default_factory=list)
    outputs: list[Output] = field(default_factory=list)
    uses_dispositions: bool = False
    extra_attrs: dict[str, str] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.flow.ref}\\{self.name}"

    def add_input(self, name: str, upstream: Output | None = None, path_name: str | None = None,
                  has_side_effects: bool = False) -> Input:
        inp = Input(self, name, upstream, has_side_effects)
        self.inputs.append(inp)
        if upstream is not None:
            self.flow.paths.append((path_name or upstream.name, upstream, inp))
        return inp

    def add_output(self, name: str, **kwargs) -> Output:
        out = Output(self, name, **kwargs)
        self.outputs.append(out)
        return out

    def add_error_output(self, sync_input: Input | None = None) -> Output:
        err = self.add_output(f"{self.description} Error Output", sync_input=sync_input, is_error=True)
        err.add("ErrorCode", T.I4, extra={"specialFlags": "1"})
        err.add("ErrorColumn", T.I4, extra={"specialFlags": "2"})
        return err

    # -- XML -------------------------------------------------------------- #
    def to_element(self) -> ET.Element:
        attrs = {"refId": self.ref, "componentClassID": self.class_id, "description": self.description,
                 "name": self.name, "version": str(self.version)}
        if self.uses_dispositions:
            attrs["usesDispositions"] = "true"
        attrs.update(self.extra_attrs)
        el = ET.Element("component", attrs)
        _props(el, self.props)
        if self.connection:
            conns = ET.SubElement(el, "connections")
            conn_name, cm = self.connection
            cm_ref = f"Package.ConnectionManagers[{cm}]"
            ET.SubElement(conns, "connection", {
                "refId": f"{self.ref}.Connections[{conn_name}]",
                "connectionManagerID": cm_ref,
                "connectionManagerRefId": cm_ref,
                "name": conn_name,
            })
        if self.inputs:
            inputs_el = ET.SubElement(el, "inputs")
            for inp in self.inputs:
                iattrs = {"refId": inp.ref, "name": inp.name}
                if inp.has_side_effects:
                    iattrs["hasSideEffects"] = "true"
                iel = ET.SubElement(inputs_el, "input", iattrs)
                if inp.columns:
                    cols_el = ET.SubElement(iel, "inputColumns")
                    for own, src, props, extra in inp.columns:
                        cel = ET.SubElement(cols_el, "inputColumn", {
                            "refId": own.lineage, **_cached(src.attrs), "cachedName": src.name,
                            **extra, "lineageId": src.lineage})
                        _props(cel, props)
                ext_el = ET.SubElement(iel, "externalMetadataColumns", {"isUsed": "True"} if inp.external else {})
                for ext in inp.external:
                    ET.SubElement(ext_el, "externalMetadataColumn", {"refId": ext.lineage, **ext.attrs, "name": ext.name})
        if self.outputs:
            outputs_el = ET.SubElement(el, "outputs")
            for out in self.outputs:
                oattrs = {"refId": out.ref}
                if out.exclusion_group is not None:
                    oattrs["exclusionGroup"] = str(out.exclusion_group)
                if out.is_error:
                    oattrs["isErrorOut"] = "true"
                if out.is_sorted:
                    oattrs["isSorted"] = "true"
                oattrs["name"] = out.name
                if out.sync_input is not None:
                    oattrs["synchronousInputId"] = out.sync_input.ref
                oel = ET.SubElement(outputs_el, "output", oattrs)
                _props(oel, out.props)
                if out.columns:
                    cols_el = ET.SubElement(oel, "outputColumns")
                    for col, props, extra in out.columns:
                        cel = ET.SubElement(cols_el, "outputColumn", {
                            "refId": col.lineage, **col.attrs, **extra, "lineageId": col.lineage, "name": col.name})
                        _props(cel, props)
                ext_el = ET.SubElement(oel, "externalMetadataColumns", {"isUsed": "True"} if out.external else {})
                for ext in out.external:
                    ET.SubElement(ext_el, "externalMetadataColumn", {"refId": ext.lineage, **ext.attrs, "name": ext.name})
        return el


_COLUMN_REF = re.compile(r"(?<![@\w])\[([^\]\[]+)\]")


class DataFlow:
    """Builder for a ``Microsoft.Pipeline`` (Data Flow Task) body."""

    def __init__(self, ref: str, pkg: "Package"):
        self.ref = ref
        self.pkg = pkg
        self.components: list[Component] = []
        self.paths: list[tuple[str, Output, Input]] = []

    def _comp(self, name: str, class_id: str, description: str, **kwargs) -> Component:
        comp = Component(self, name, class_id, description, **kwargs)
        self.components.append(comp)
        return comp

    def _bind_expression(self, friendly: str, inp: Input) -> str:
        """Translate ``[Col]`` references into ``#{lineage}`` and register input columns."""
        names = {c.name: c for c in inp.available()}

        def repl(match: re.Match[str]) -> str:
            col = names.get(match.group(1))
            if col is None:
                return match.group(0)
            inp.use(col)
            return "#{" + col.lineage + "}"

        return _COLUMN_REF.sub(repl, friendly)

    # -- Sources ---------------------------------------------------------- #
    def ole_src(self, name: str, cm: str, sql: str, columns: Sequence[tuple[str, dict]],
                params: Sequence[str] = ()) -> Output:
        props = [
            ("CommandTimeout", "0", "System.Int32"),
            ("OpenRowset", ""),
            ("OpenRowsetVariable", None),
            ("SqlCommand", sql, "System.String", {"UITypeEditor": "Microsoft.DataTransformationServices.Controls.ModalMultilineStringEditor"}),
            ("SqlCommandVariable", None),
            ("DefaultCodePage", "1252", "System.Int32"),
            ("AlwaysUseDefaultCodePage", "false", "System.Boolean"),
            ("AccessMode", "2", "System.Int32", {"typeConverter": "AccessMode"}),
            ("ParameterMapping", "".join(f'"Parameter{i}:Input",{self.pkg.variable_id(p)};' for i, p in enumerate(params)) or None),
        ]
        comp = self._comp(name, "Microsoft.OLEDBSource", "OLE DB Source", version=7, props=props,
                          connection=("OleDbConnection", cm), uses_dispositions=True)
        out = comp.add_output("OLE DB Source Output")
        for col_name, attrs in columns:
            out.add(col_name, attrs, extra={"errorOrTruncationOperation": "Conversion",
                                            "errorRowDisposition": "FailComponent",
                                            "truncationRowDisposition": "FailComponent"}, external=True)
        comp.add_error_output()
        return out

    def flat_src(self, name: str, cm: str, columns: Sequence[tuple[str, dict]]) -> Output:
        props = [
            ("RetainNulls", "false", "System.Boolean"),
            ("FileNameColumnName", None),
        ]
        comp = self._comp(name, "Microsoft.FlatFileSource", "Flat File Source", version=1, props=props,
                          connection=("FlatFileConnection", cm), uses_dispositions=True)
        out = comp.add_output("Flat File Source Output")
        for col_name, attrs in columns:
            out.add(col_name, attrs, props=[("FastParse", "false", "System.Boolean"),
                                            ("UseBinaryFormat", "false", "System.Boolean")],
                    extra={"errorOrTruncationOperation": "Conversion", "errorRowDisposition": "FailComponent",
                           "truncationRowDisposition": "FailComponent"}, external=True)
        comp.add_error_output()
        return out

    # -- Transforms ------------------------------------------------------- #
    def derived(self, name: str, upstream: Output, new_cols: Sequence[tuple[str, str, dict]]) -> Output:
        comp = self._comp(name, "Microsoft.DerivedColumn", "Derived Column", version=0, uses_dispositions=True)
        inp = comp.add_input("Derived Column Input", upstream)
        out = comp.add_output("Derived Column Output", sync_input=inp, exclusion_group=0)
        for col_name, friendly, attrs in new_cols:
            out.add(col_name, attrs, props=[
                ("Expression", self._bind_expression(friendly, inp), "System.String",
                 {"containsID": "true", "description": "Derived Column Expression"}),
                ("FriendlyExpression", friendly, "System.String",
                 {"containsID": "true", "description": "Derived Column Friendly Expression", "expressionType": "Notify"}),
            ], extra={"errorOrTruncationOperation": "Computation", "errorRowDisposition": "FailComponent",
                      "truncationRowDisposition": "FailComponent"})
        comp.add_error_output(inp)
        return out

    def data_convert(self, name: str, upstream: Output, conversions: Sequence[tuple[str, str, dict]]) -> Output:
        comp = self._comp(name, "Microsoft.DataConvert", "Data Conversion", version=0, uses_dispositions=True)
        inp = comp.add_input("Data Conversion Input", upstream)
        out = comp.add_output("Data Conversion Output", sync_input=inp, exclusion_group=0)
        for src, new_name, attrs in conversions:
            in_col = inp.use(upstream.col(src))
            out.add(new_name, attrs, props=[
                ("SourceInputColumnLineageID", "#{" + upstream.col(src).lineage + "}", "System.Int32", {"containsID": "true"}),
                ("FastParse", "false", "System.Boolean"),
            ], extra={"errorOrTruncationOperation": "Conversion", "errorRowDisposition": "RedirectRow",
                      "truncationRowDisposition": "RedirectRow"})
            _ = in_col
        comp.add_error_output(inp)
        return out

    def cond_split(self, name: str, upstream: Output, cases: Sequence[tuple[str, str]],
                   default_name: str = "Conditional Split Default Output") -> dict[str, Output]:
        comp = self._comp(name, "Microsoft.ConditionalSplit", "Conditional Split", version=0, uses_dispositions=True)
        inp = comp.add_input("Conditional Split Input", upstream)
        result: dict[str, Output] = {}
        for order, (case_name, friendly) in enumerate(cases):
            result[case_name] = comp.add_output(case_name, sync_input=inp, exclusion_group=1, props=[
                ("EvaluationOrder", str(order), "System.Int32"),
                ("FriendlyExpression", friendly, "System.String", {"expressionType": "Notify"}),
                ("Expression", self._bind_expression(friendly, inp), "System.String", {"containsID": "true"}),
                ("IsDefaultOut", "false", "System.Boolean"),
            ])
        result[default_name] = comp.add_output(default_name, sync_input=inp, exclusion_group=1, props=[
            ("IsDefaultOut", "true", "System.Boolean")])
        comp.add_error_output(inp)
        return result

    def lookup(self, name: str, upstream: Output, cm: str, sql: str, joins: Sequence[tuple[str, str]],
               copy: Sequence[tuple[str, str, dict]], cache_type: int = 0) -> tuple[Output, Output]:
        ref_cols = [(rc, None) for _, rc in joins] + [(rc, attrs) for rc, _, attrs in copy]
        metadata = "<referenceMetadata><referenceColumns>" + "".join(
            f'<referenceColumn name="{rc}" />' for rc, _ in ref_cols) + "</referenceColumns></referenceMetadata>"
        comp = self._comp(name, "Microsoft.Lookup", "Lookup", version=6, uses_dispositions=True,
                          connection=("OleDbConnection", cm), props=[
                              ("SqlCommand", sql, "System.String", {"UITypeEditor": "Microsoft.DataTransformationServices.Controls.ModalMultilineStringEditor"}),
                              ("SqlCommandParam", None),
                              ("ConnectionType", "0", "System.Int32", {"typeConverter": "LookupConnectionType"}),
                              ("CacheType", str(cache_type), "System.Int32", {"typeConverter": "CacheType"}),
                              ("NoMatchBehavior", "1", "System.Int32", {"typeConverter": "LookupNoMatchBehavior"}),
                              ("NoMatchCachePercentage", "0", "System.Int32"),
                              ("ReferenceMetadataXml", metadata),
                              ("DefaultCodePage", "1252", "System.Int32"),
                              ("MaxMemoryUsage", "25", "System.Int32"),
                          ])
        inp = comp.add_input("Lookup Input", upstream)
        for in_col, ref_col in joins:
            inp.use(upstream.col(in_col), props=[("JoinToReferenceColumn", ref_col),
                                                 ("CopyFromReferenceColumn", None)])
        match = comp.add_output("Lookup Match Output", sync_input=inp, exclusion_group=1)
        for ref_col, out_name, attrs in copy:
            match.add(out_name, attrs, props=[("CopyFromReferenceColumn", ref_col)],
                      extra={"errorOrTruncationOperation": "Copy Column", "errorRowDisposition": "FailComponent",
                             "truncationRowDisposition": "FailComponent"})
        no_match = comp.add_output("Lookup No Match Output", sync_input=inp, exclusion_group=1)
        comp.add_error_output(inp)
        return match, no_match

    def row_count(self, name: str, upstream: Output, variable: str) -> Output:
        comp = self._comp(name, "Microsoft.RowCount", "Row Count", version=0,
                          props=[("VariableName", variable)])
        inp = comp.add_input("Row Count Input 1", upstream)
        return comp.add_output("Row Count Output 1", sync_input=inp)

    def multicast(self, name: str, upstream: Output, count: int) -> list[Output]:
        comp = self._comp(name, "Microsoft.Multicast", "Multicast", version=0)
        inp = comp.add_input("Multicast Input 1", upstream)
        return [comp.add_output(f"Multicast Output {i}", sync_input=inp) for i in range(1, count + 1)]

    def union_all(self, name: str, upstreams: Sequence[Output], columns: Sequence[tuple[str, dict]]) -> Output:
        comp = self._comp(name, "Microsoft.UnionAll", "Union All", version=0)
        out = comp.add_output("Union All Output 1")
        out_cols = {col_name: out.add(col_name, attrs) for col_name, attrs in columns}
        for idx, upstream in enumerate(upstreams, start=1):
            inp = comp.add_input(f"Union All Input {idx}", upstream)
            for col_name, oc in out_cols.items():
                try:
                    src = upstream.col(col_name)
                except KeyError:
                    continue  # SSIS emits <ignore> for unmapped columns
                inp.use(src, props=[("OutputColumnLineageID", "#{" + oc.lineage + "}", "System.Int32",
                                     {"containsID": "true"})])
        return out

    def aggregate(self, name: str, upstream: Output, spec: Sequence[tuple[str, str, str | None, dict]]) -> Output:
        agg_types = {"GroupBy": "0", "Count": "1", "CountAll": "2", "CountDistinct": "3",
                     "Sum": "4", "Average": "5", "Min": "6", "Max": "7"}
        comp = self._comp(name, "Microsoft.Aggregate", "Aggregate", version=3, props=[
            ("KeyScale", "0", "System.Int32"), ("Keys", None), ("CountDistinctScale", "0", "System.Int32"),
            ("AutoExtendFactor", "25", "System.Int32")])
        inp = comp.add_input("Aggregate Input 1", upstream)
        out = comp.add_output("Aggregate Output 1")
        for out_name, op, src, attrs in spec:
            props: list[tuple] = [("AggregationType", agg_types[op], "System.Int32", {"typeConverter": "AggregationType"}),
                                  ("IsBig", "0", "System.Int32")]
            if src:
                src_col = upstream.col(src)
                inp.use(src_col)
                props.insert(0, ("AggregationColumnId", "#{" + src_col.lineage + "}", "System.Int32", {"containsID": "true"}))
            out.add(out_name, attrs, props=props)
        return out

    def sort(self, name: str, upstream: Output, keys: Sequence[str], remove_duplicates: bool = False) -> Output:
        comp = self._comp(name, "Microsoft.Sort", "Sort", version=0, props=[
            ("EliminateDuplicates", str(remove_duplicates).lower(), "System.Boolean"),
            ("MaximumThreads", "-1", "System.Int32")])
        inp = comp.add_input("Sort Input", upstream)
        out = comp.add_output("Sort Output", is_sorted=True)
        for col in upstream.available():
            pos = keys.index(col.name) + 1 if col.name in keys else 0
            inp.use(col, props=[("NewSortKeyPosition", str(pos), "System.Int32"),
                                ("NewComparisonFlags", "0", "System.Int32")])
            extra = {"sortKeyPosition": str(pos)} if pos else {}
            out.add(col.name, col.attrs, props=[("SortColumnId", "#{" + col.lineage + "}", "System.Int32",
                                                 {"containsID": "true"})], extra=extra)
        return out

    def merge_join(self, name: str, left: Output, right: Output, join_type: str, key_count: int,
                   left_cols: Sequence[tuple[str, str]], right_cols: Sequence[tuple[str, str]]) -> Output:
        types = {"FullOuter": "0", "LeftOuter": "1", "Inner": "2"}
        comp = self._comp(name, "Microsoft.MergeJoin", "Merge Join", version=2, props=[
            ("JoinType", types[join_type], "System.Int32", {"typeConverter": "JoinType"}),
            ("NumKeyColumns", str(key_count), "System.Int32"),
            ("TreatNullsAsEqual", "true", "System.Boolean"),
            ("MaxBuffersPerInput", "5", "System.Int32")])
        left_in = comp.add_input("Merge Join Left Input", left)
        right_in = comp.add_input("Merge Join Right Input", right)
        out = comp.add_output("Merge Join Output")
        for inp, upstream, pairs in ((left_in, left, left_cols), (right_in, right, right_cols)):
            for src, out_name in pairs:
                in_col = inp.use(upstream.col(src))
                out.add(out_name, upstream.col(src).attrs, props=[
                    ("InputColumnID", "#{" + in_col.lineage + "}", "System.Int32", {"containsID": "true"})])
        return out

    def ole_cmd(self, name: str, upstream: Output, cm: str, sql: str, params: Sequence[str]) -> Output:
        comp = self._comp(name, "Microsoft.OLEDBCommand", "OLE DB Command", version=2, uses_dispositions=True,
                          connection=("OleDbConnection", cm), props=[
                              ("CommandTimeout", "0", "System.Int32"),
                              ("SqlCommand", sql, "System.String", {"UITypeEditor": "Microsoft.DataTransformationServices.Controls.ModalMultilineStringEditor"}),
                              ("DefaultCodePage", "1252", "System.Int32")])
        inp = comp.add_input("OLE DB Command Input", upstream)
        for i, col in enumerate(params):
            inp.use(upstream.col(col), external_name=f"Param_{i}")
        out = comp.add_output("OLE DB Command Output", sync_input=inp, exclusion_group=1)
        comp.add_error_output(inp)
        return out

    def scd(self, name: str, upstream: Output, cm: str, table: str, business_keys: Sequence[str],
            changing: Sequence[str], historical: Sequence[str], fixed: Sequence[str] = ()) -> dict[str, Output]:
        column_types = {**{c: "1" for c in business_keys}, **{c: "2" for c in changing},
                        **{c: "3" for c in historical}, **{c: "4" for c in fixed}}
        select_cols = ", ".join(f"[{c}]" for c in column_types)
        comp = self._comp(name, "Microsoft.SCD", "Slowly Changing Dimension", version=1, uses_dispositions=True,
                          connection=("OleDbConnection", cm), props=[
                              ("SQLCommand", f"SELECT {select_cols} FROM {table} WHERE [IsCurrent] = 1"),
                              ("CurrentRowWhere", "[IsCurrent] = 1"),
                              ("EnableInferredMember", "false", "System.Boolean"),
                              ("FailOnFixedAttributeChange", "true", "System.Boolean"),
                              ("FailOnLookupFailure", "false", "System.Boolean"),
                              ("IncomingRowChangeType", "1", "System.Int32"),
                              ("UpdateChangingAttributeHistory", "true", "System.Boolean")])
        inp = comp.add_input("Slowly Changing Dimension Input", upstream)
        for col_name, col_type in column_types.items():
            inp.use(upstream.col(col_name), props=[("ColumnType", col_type, "System.Int32", {"typeConverter": "ColumnType"})])
        outputs = {}
        for out_name in ("Unchanged Output", "New Output", "Fixed Attribute Output",
                         "Changing Attribute Updates Output", "Historical Attribute Inserts Output",
                         "Inferred Member Updates Output"):
            outputs[out_name] = comp.add_output(out_name, sync_input=inp, exclusion_group=1)
        return outputs

    def fuzzy_lookup(self, name: str, upstream: Output, cm: str, ref_table: str, join: tuple[str, str],
                     copy: Sequence[tuple[str, str, dict]], min_similarity: float) -> Output:
        comp = self._comp(name, "Microsoft.BestMatch", "Fuzzy Lookup", version=1,
                          connection=("OleDbConnection", cm), props=[
                              ("ReferenceTableName", ref_table),
                              ("MatchIndexOptions", "1", "System.Int32", {"typeConverter": "MatchIndexOptions"}),
                              ("MatchIndexName", "cmp.SdnEntry_FuzzyIndex"),
                              ("MinSimilarity", str(min_similarity), "System.Double"),
                              ("MaxOutputMatchesPerInput", "3", "System.Int32"),
                              ("Exhaustive", "false", "System.Boolean"),
                              ("WarmCaches", "true", "System.Boolean"),
                              ("Delimiters", " ,.;:-\"'&/\\@!?()<>[]{}|#*^%")])
        inp = comp.add_input("Fuzzy Lookup Input", upstream)
        inp.use(upstream.col(join[0]), props=[("JoinToReferenceColumn", join[1]),
                                              ("JoinType", "2", "System.Int32"),
                                              ("MinSimilarity", str(min_similarity), "System.Double")])
        out = comp.add_output("Fuzzy Lookup Output", sync_input=None)
        for col in upstream.available():
            inp.use(col)
            out.add(col.name, col.attrs, props=[("SourceInputColumnLineageID", "#{" + col.lineage + "}",
                                                 "System.Int32", {"containsID": "true"})])
        for ref_col, out_name, attrs in copy:
            out.add(out_name, attrs, props=[("CopyFromReferenceColumn", ref_col)])
        out.add("_Similarity", T.R4, props=[("ColumnType", "1", "System.Int32")])
        out.add("_Confidence", T.R4, props=[("ColumnType", "2", "System.Int32")])
        out.add(f"_Similarity_{join[0]}", T.R4, props=[("ColumnType", "3", "System.Int32")])
        return out

    def script_component(self, name: str, upstream: Output, code: str, out_cols: Sequence[tuple[str, dict]],
                         read_only_vars: str = "", project_name: str = "SC_Component") -> Output:
        comp = self._comp(name, "Microsoft.ManagedComponentHost", "Script Component", version=10, props=[
            ("SourceCode", ["\\main.cs", code, "\\SC_Component.csproj", _csproj(project_name)]),
            ("BinaryCode", [], "System.String"),
            ("VSTAProjectName", project_name),
            ("ScriptLanguage", "CSharp", "System.String", {"typeConverter": "ScriptLanguage"}),
            ("ReadOnlyVariables", read_only_vars),
            ("ReadWriteVariables", ""),
            ("BreakpointCollection", None),
            ("UserComponentTypeName", "Microsoft.ScriptComponentHost"),
        ])
        inp = comp.add_input("Input 0", upstream)
        for col in upstream.available():
            inp.use(col)
        out = comp.add_output("Output 0", props=[("SynchronousInputID", "0", "System.Int32")])
        for col_name, attrs in out_cols:
            out.add(col_name, attrs)
        return out

    # -- Destinations ----------------------------------------------------- #
    def ole_dest(self, name: str, upstream: Output, cm: str, table: str,
                 mapping: Sequence[tuple[str, str]] | None = None, fast_load: bool = True) -> Component:
        comp = self._comp(name, "Microsoft.OLEDBDestination", "OLE DB Destination", version=4,
                          uses_dispositions=True, connection=("OleDbConnection", cm), props=[
                              ("CommandTimeout", "0", "System.Int32"),
                              ("OpenRowset", table),
                              ("OpenRowsetVariable", None),
                              ("SqlCommand", None),
                              ("DefaultCodePage", "1252", "System.Int32"),
                              ("AlwaysUseDefaultCodePage", "false", "System.Boolean"),
                              ("AccessMode", "3" if fast_load else "0", "System.Int32", {"typeConverter": "AccessMode"}),
                              ("FastLoadKeepIdentity", "false", "System.Boolean"),
                              ("FastLoadKeepNulls", "false", "System.Boolean"),
                              ("FastLoadOptions", "TABLOCK,CHECK_CONSTRAINTS" if fast_load else None),
                              ("FastLoadMaxInsertCommitSize", "2147483647", "System.Int32")])
        inp = comp.add_input("OLE DB Destination Input", upstream, has_side_effects=True)
        pairs = mapping if mapping is not None else [(c.name, c.name) for c in upstream.available()]
        for src, ext in pairs:
            inp.use(upstream.col(src), external_name=ext)
        comp.add_error_output(inp)
        return comp

    def flat_dest(self, name: str, upstream: Output, cm: str, columns: Sequence[str] | None = None,
                  header: str | None = None) -> Component:
        comp = self._comp(name, "Microsoft.FlatFileDestination", "Flat File Destination", version=1,
                          connection=("FlatFileConnection", cm), props=[
                              ("Overwrite", "true", "System.Boolean"),
                              ("Header", header)])
        inp = comp.add_input("Flat File Destination Input", upstream, has_side_effects=True)
        cols = columns if columns is not None else [c.name for c in upstream.available()]
        for col in cols:
            inp.use(upstream.col(col), external_name=col)
        return comp

    # -- XML -------------------------------------------------------------- #
    def to_element(self) -> ET.Element:
        pipeline = ET.Element("pipeline", {"version": "1"})
        comps = ET.SubElement(pipeline, "components")
        for comp in self.components:
            comps.append(comp.to_element())
        if self.paths:
            paths = ET.SubElement(pipeline, "paths")
            seen: Counter[str] = Counter()
            for path_name, out, inp in self.paths:
                seen[path_name] += 1
                if seen[path_name] > 1:
                    path_name = f"{path_name} {seen[path_name] - 1}"
                ET.SubElement(paths, "path", {"refId": f"{self.ref}.Paths[{path_name}]", "endId": inp.ref,
                                              "name": path_name, "startId": out.ref})
        return pipeline


def _csproj(name: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<Project ToolsVersion="15.0" DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">\n'
        f'  <PropertyGroup><AssemblyName>{name}</AssemblyName><RootNamespace>{name}</RootNamespace>'
        '<TargetFrameworkVersion>v4.7</TargetFrameworkVersion></PropertyGroup>\n'
        '  <ItemGroup><Compile Include="ScriptMain.cs" /></ItemGroup>\n'
        '</Project>\n'
    )


# --------------------------------------------------------------------------- #
# Control flow                                                                #
# --------------------------------------------------------------------------- #

class Executable:
    """An SSIS task or container element in the control flow."""

    def __init__(self, pkg: "Package", ref: str, name: str, creation_name: str, description: str,
                 is_task: bool = True, attrs: dict[str, str] | None = None):
        self.pkg = pkg
        self.ref = ref
        self.name = name
        self.creation_name = creation_name
        self.description = description
        self.is_task = is_task
        self.attrs = attrs or {}
        self.property_expressions: list[tuple[str, str]] = []
        self.object_data: ET.Element | None = None
        self.pipeline: DataFlow | None = None
        self.disabled = False

    def expr(self, prop: str, expression: str) -> "Executable":
        self.property_expressions.append((prop, expression))
        return self

    def _base_element(self) -> ET.Element:
        attrs = {d("refId"): self.ref, d("CreationName"): self.creation_name}
        attrs[d("Description")] = self.description
        if self.disabled:
            attrs[d("Disabled")] = "True"
        attrs[d("DTSID")] = guid(self.pkg.name, self.ref)
        attrs[d("ExecutableType")] = self.creation_name
        for key, value in self.attrs.items():
            attrs[d(key)] = value
        attrs[d("LocaleID")] = "-1"
        attrs[d("ObjectName")] = self.name
        if self.is_task:
            attrs[d("TaskContact")] = TASK_CONTACTS.get(self.creation_name, "")
            attrs[d("ThreadHint")] = "0"
        el = ET.Element(d("Executable"), attrs)
        for prop, expression in self.property_expressions:
            ET.SubElement(el, d("PropertyExpression"), {d("Name"): prop}).text = expression
        return el

    def to_element(self) -> ET.Element:
        el = self._base_element()
        ET.SubElement(el, d("Variables"))
        data = ET.SubElement(el, d("ObjectData"))
        if self.pipeline is not None:
            data.append(self.pipeline.to_element())
        elif self.object_data is not None:
            data.append(self.object_data)
        return el


TASK_CONTACTS = {
    "Microsoft.ExecuteSQLTask": "Execute SQL Task; Microsoft Corporation; SQL Server 2019; © 2019 Microsoft Corporation; All Rights Reserved;http://www.microsoft.com/sql/support/default.asp;1",
    "Microsoft.Pipeline": "Performs high-performance data extraction, transformation and loading;Microsoft Corporation; Microsoft SQL Server; (C) Microsoft Corporation; All Rights Reserved;http://www.microsoft.com/sql/support/default.asp;1",
    "Microsoft.ScriptTask": "",
    "Microsoft.SendMailTask": "Send Mail Task; Microsoft Corporation; SQL Server 2019; © 2019 Microsoft Corporation; All Rights Reserved;http://www.microsoft.com/sql/support/default.asp;1",
    "Microsoft.FileSystemTask": "File System Task;Microsoft Corporation; SQL Server 2019; © 2019 Microsoft Corporation; All Rights Reserved;http://www.microsoft.com/sql/support/default.asp;1",
    "Microsoft.ExecuteProcess": "Execute Process Task;Microsoft Corporation; SQL Server 2019; © 2019 Microsoft Corporation; All Rights Reserved;http://www.microsoft.com/sql/support/default.asp;1",
    "Microsoft.ExecutePackageTask": "Microsoft Corporation; Microsoft SQL Server; Microsoft Corporation; All Rights Reserved;http://www.microsoft.com/sql/support/default.asp;1",
}


@dataclass
class _Variable:
    namespace: str
    name: str
    dtype: str
    value: str
    expression: str | None
    description: str


class Container(Executable):
    """Package, Sequence, ForEach, For Loop or event handler scope."""

    def __init__(self, pkg: "Package", ref: str, name: str, creation_name: str, description: str,
                 attrs: dict[str, str] | None = None):
        super().__init__(pkg, ref, name, creation_name, description, is_task=False, attrs=attrs)
        self.variables: list[_Variable] = []
        self.executables: list[Executable] = []
        self.constraints: list[ET.Element] = []
        self.enumerator: ET.Element | None = None
        self.variable_mappings: list[tuple[int, str]] = []

    # -- helpers ---------------------------------------------------------- #
    def _child_ref(self, name: str) -> str:
        return f"{self.ref}\\{name}"

    def _add(self, exe: Executable) -> Executable:
        if any(e.name == exe.name for e in self.executables):
            raise ValueError(f"Duplicate executable name {exe.name!r} in {self.ref}")
        self.executables.append(exe)
        return exe

    def variable(self, name: str, dtype: str, value: str = "", expression: str | None = None,
                 description: str = "", namespace: str = "User") -> str:
        self.variables.append(_Variable(namespace, name, dtype, value, expression, description))
        return f"{namespace}::{name}"

    # -- tasks ------------------------------------------------------------ #
    def execute_sql(self, name: str, cm: str, sql: str, *, result_type: str = "None",
                    results: Sequence[tuple[str, str]] = (), params: Sequence[tuple[str, str, str, str]] = (),
                    description: str = "Execute SQL Task", timeout: int = 0) -> Executable:
        """params: (variable, direction Input|Output|ReturnValue, OLE DB type, parameter name)."""
        exe = Executable(self.pkg, self._child_ref(name), name, "Microsoft.ExecuteSQLTask", description)
        attrs = {sq("Connection"): self.pkg.cm_id(cm)}
        if timeout:
            attrs[sq("TimeOut")] = str(timeout)
        attrs[sq("SqlStatementSource")] = sql
        if result_type != "None":
            attrs[sq("ResultType")] = f"ResultSetType_{result_type}"
        data = ET.Element(sq("SqlTaskData"), attrs)
        for result_name, var in results:
            ET.SubElement(data, sq("ResultBinding"), {sq("ResultName"): result_name, sq("DtsVariableName"): var})
        for var, direction, ole_type, param_name in params:
            ET.SubElement(data, sq("ParameterBinding"), {
                sq("ParameterName"): param_name, sq("DtsVariableName"): var,
                sq("ParameterDirection"): direction, sq("DataType"): OLEDB_PARAM_TYPES[ole_type],
                sq("ParameterSize"): "-1"})
        exe.object_data = data
        return self._add(exe)

    def data_flow(self, name: str, description: str = "Data Flow Task") -> tuple[Executable, DataFlow]:
        exe = Executable(self.pkg, self._child_ref(name), name, "Microsoft.Pipeline", description)
        exe.pipeline = DataFlow(exe.ref, self.pkg)
        self._add(exe)
        return exe, exe.pipeline

    def script_task(self, name: str, code: str, *, read_only: str = "", read_write: str = "",
                    description: str = "Script Task") -> Executable:
        exe = Executable(self.pkg, self._child_ref(name), name, "Microsoft.ScriptTask", description)
        project = "ST_" + guid(self.pkg.name, exe.ref).strip("{}").replace("-", "").lower()[:32]
        sp = ET.Element("ScriptProject", {"Name": project, "VSTAMajorVersion": "15", "VSTAMinorVersion": "0",
                                          "Language": "CSharp", "ReadOnlyVariables": read_only,
                                          "ReadWriteVariables": read_write})
        ET.SubElement(sp, "ProjectItem", {"Name": "ScriptMain.cs", "Encoding": "UTF8"}).text = code
        ET.SubElement(sp, "ProjectItem", {"Name": f"{project}.csproj", "Encoding": "UTF8"}).text = _csproj(project)
        exe.object_data = sp
        return self._add(exe)

    def send_mail(self, name: str, smtp_cm: str, *, sender: str, to: str, subject: str, body: str,
                  description: str = "Send Mail Task", priority: str = "Normal") -> Executable:
        exe = Executable(self.pkg, self._child_ref(name), name, "Microsoft.SendMailTask", description)
        exe.object_data = ET.Element(sm("SendMailTaskData"), {
            sm("SMTPServer"): self.pkg.cm_id(smtp_cm), sm("From"): sender, sm("To"): to,
            sm("Subject"): subject, sm("Priority"): priority, sm("MessageSource"): body})
        return self._add(exe)

    def file_system(self, name: str, operation: str, *, source: str, source_is_var: bool,
                    destination: str = "", destination_is_var: bool = False, overwrite: bool = True,
                    description: str = "File System Task") -> Executable:
        exe = Executable(self.pkg, self._child_ref(name), name, "Microsoft.FileSystemTask", description)
        exe.object_data = ET.Element("FileSystemData", {
            "TaskOperationType": operation, "TaskSourcePath": source,
            "TaskIsSourceVariable": str(source_is_var), "TaskDestinationPath": destination,
            "TaskIsDestinationVariable": str(destination_is_var), "TaskOverwriteDestFile": str(overwrite)})
        return self._add(exe)

    def execute_process(self, name: str, *, executable: str, arguments: str, working_dir: str = "",
                        timeout: int = 0, success_value: int = 0, description: str = "Execute Process Task") -> Executable:
        exe = Executable(self.pkg, self._child_ref(name), name, "Microsoft.ExecuteProcess", description)
        attrs = {"Executable": executable, "Arguments": arguments, "WorkingDirectory": working_dir,
                 "SuccessValue": str(success_value)}
        if timeout:
            attrs["TimeOut"] = str(timeout)
            attrs["TerminateAfterTimeOut"] = "True"
        exe.object_data = ET.Element("ExecuteProcessData", attrs)
        return self._add(exe)

    def execute_package(self, name: str, package_name: str, assignments: Sequence[tuple[str, str]] = (),
                        description: str = "Execute Package Task") -> Executable:
        exe = Executable(self.pkg, self._child_ref(name), name, "Microsoft.ExecutePackageTask", description)
        root = ET.Element("ExecutePackageTask")
        ET.SubElement(root, "UseProjectReference").text = "True"
        ET.SubElement(root, "PackageName").text = package_name
        for param, binding in assignments:
            pa = ET.SubElement(root, "ParameterAssignment")
            ET.SubElement(pa, "ParameterName").text = param
            ET.SubElement(pa, "BindedVariableOrParameterName").text = binding
        exe.object_data = root
        return self._add(exe)

    # -- containers ------------------------------------------------------- #
    def sequence(self, name: str, description: str = "Sequence Container") -> "Container":
        return self._add(Container(self.pkg, self._child_ref(name), name, "STOCK:SEQUENCE", description))  # type: ignore[return-value]

    def for_loop(self, name: str, *, init: str, eval_: str, assign: str,
                 description: str = "For Loop Container") -> "Container":
        c = Container(self.pkg, self._child_ref(name), name, "STOCK:FORLOOP", description,
                      attrs={"AssignExpression": assign, "EvalExpression": eval_, "InitExpression": init,
                             "MaxConcurrent": "1"})
        return self._add(c)  # type: ignore[return-value]

    def foreach_file(self, name: str, *, folder: str, file_spec: str, mappings: Sequence[str],
                     folder_expression: str | None = None, retrieval: int = 0,
                     description: str = "Foreach Loop Container") -> "Container":
        c = Container(self.pkg, self._child_ref(name), name, "STOCK:FOREACHLOOP", description)
        enum = ET.Element(d("ForEachEnumerator"), {
            d("refId"): f"{c.ref}\\{{{guid(self.pkg.name, c.ref, 'enum')[1:-1]}}}",
            d("CreationName"): "Microsoft.ForEachFileEnumerator",
            d("DTSID"): guid(self.pkg.name, c.ref, "enumerator"),
            d("ObjectName"): guid(self.pkg.name, c.ref, "enum")})
        if folder_expression:
            ET.SubElement(enum, d("PropertyExpression"), {d("Name"): "Directory"}).text = folder_expression
        od = ET.SubElement(enum, d("ObjectData"))
        props = ET.SubElement(od, "ForEachFileEnumeratorProperties")
        for key, val in (("Folder", folder), ("FileSpec", file_spec), ("FileNameRetrievalType", str(retrieval)),
                         ("Recurse", "0")):
            ET.SubElement(props, "FEFEProperty", {key: val})
        c.enumerator = enum
        c.variable_mappings = list(enumerate(mappings))
        return self._add(c)  # type: ignore[return-value]

    def foreach_ado(self, name: str, *, source_variable: str, mappings: Sequence[str],
                    description: str = "Foreach Loop Container") -> "Container":
        c = Container(self.pkg, self._child_ref(name), name, "STOCK:FOREACHLOOP", description)
        enum = ET.Element(d("ForEachEnumerator"), {
            d("refId"): f"{c.ref}\\{{{guid(self.pkg.name, c.ref, 'enum')[1:-1]}}}",
            d("CreationName"): "Microsoft.ForEachADOEnumerator",
            d("DTSID"): guid(self.pkg.name, c.ref, "enumerator"),
            d("ObjectName"): guid(self.pkg.name, c.ref, "enum")})
        od = ET.SubElement(enum, d("ObjectData"))
        ET.SubElement(od, "FEEADO", {"EnumType": "EnumerateRowsInFirstTable", "VarName": source_variable})
        c.enumerator = enum
        c.variable_mappings = list(enumerate(mappings))
        return self._add(c)  # type: ignore[return-value]

    # -- precedence constraints ------------------------------------------ #
    def constraint(self, frm: Executable, to: Executable, value: str = "Success", *,
                   expression: str | None = None, eval_op: str | None = None,
                   logical_and: bool = True) -> None:
        idx = len(self.constraints)
        name = "Constraint" if idx == 0 else f"Constraint {idx}"
        attrs = {d("refId"): f"{self.ref}.PrecedenceConstraints[{name}]", d("CreationName"): "",
                 d("DTSID"): guid(self.pkg.name, self.ref, name)}
        if expression is not None:
            attrs[d("EvalOp")] = EVAL_OPS[eval_op or "ExpressionAndConstraint"]
            attrs[d("Expression")] = expression
        elif eval_op:
            attrs[d("EvalOp")] = EVAL_OPS[eval_op]
        attrs[d("From")] = frm.ref
        attrs[d("LogicalAnd")] = "True" if logical_and else "False"
        attrs[d("ObjectName")] = name
        attrs[d("To")] = to.ref
        val = PRECEDENCE_VALUES[value]
        if val is not None:
            attrs[d("Value")] = val
        self.constraints.append(ET.Element(d("PrecedenceConstraint"), attrs))

    def chain(self, *exes: Executable) -> None:
        for a, b in zip(exes, exes[1:]):
            self.constraint(a, b)

    # -- XML -------------------------------------------------------------- #
    def _variables_element(self) -> ET.Element:
        holder = ET.Element(d("Variables"))
        for v in self.variables:
            attrs = {d("CreationName"): "", d("Description"): v.description,
                     d("DTSID"): guid(self.pkg.name, self.ref, v.namespace, v.name)}
            if v.expression is not None:
                attrs[d("EvaluateAsExpression")] = "True"
                attrs[d("Expression")] = v.expression
            attrs[d("IncludeInDebugDump")] = "6789" if v.dtype == "Object" else "2345"
            attrs[d("Namespace")] = v.namespace
            attrs[d("ObjectName")] = v.name
            if not v.description:
                del attrs[d("Description")]
            vel = ET.SubElement(holder, d("Variable"), attrs)
            if v.dtype == "Object":
                val = ET.SubElement(vel, d("VariableValue"), {d("DataSubType"): "ManagedSerializable",
                                                              d("DataType"): VARIABLE_TYPES["Object"]})
                env = ET.SubElement(val, f"{{{SOAP_ENV_NS}}}Envelope",
                                    {f"{{{SOAP_ENV_NS}}}encodingStyle": SOAP_ENC_NS})
                body = ET.SubElement(env, f"{{{SOAP_ENV_NS}}}Body")
                ET.SubElement(body, f"{{{XSD_NS}}}anyType", {"id": "ref-1"})
            else:
                ET.SubElement(vel, d("VariableValue"), {d("DataType"): VARIABLE_TYPES[v.dtype]}).text = v.value
        return holder

    def _fill(self, el: ET.Element) -> None:
        if self.enumerator is not None:
            el.append(self.enumerator)
        el.append(self._variables_element())
        if self.executables:
            exes = ET.SubElement(el, d("Executables"))
            for exe in self.executables:
                exes.append(exe.to_element())
        if self.constraints:
            pcs = ET.SubElement(el, d("PrecedenceConstraints"))
            pcs.extend(self.constraints)
        if self.variable_mappings:
            maps = ET.SubElement(el, d("ForEachVariableMappings"))
            for idx, var in self.variable_mappings:
                ET.SubElement(maps, d("ForEachVariableMapping"), {
                    d("refId"): f"{self.ref}.ForEachVariableMappings[Variable{'' if idx == 0 else idx}]",
                    d("CreationName"): "", d("DTSID"): guid(self.pkg.name, self.ref, "map", str(idx)),
                    d("ObjectName"): f"Variable{'' if idx == 0 else idx}",
                    d("ValueIndex"): str(idx), d("VariableName"): var})

    def to_element(self) -> ET.Element:
        el = self._base_element()
        self._fill(el)
        return el


class EventHandler(Container):
    def __init__(self, pkg: "Package", owner_ref: str, event: str):
        ref = f"{owner_ref}.EventHandlers[{event}]"
        super().__init__(pkg, ref, event, event, "")
        self.event = event
        self.variable("Propagate", "Boolean", "-1", namespace="System",
                      description="Specifies whether the event is propagated to higher level event handlers")

    def to_element(self) -> ET.Element:
        el = ET.Element(d("EventHandler"), {
            d("refId"): self.ref, d("CreationName"): self.event,
            d("DTSID"): guid(self.pkg.name, self.ref), d("EventID"): "0",
            d("EventName"): self.event, d("LocaleID"): "-1"})
        self._fill(el)
        return el


@dataclass
class ConnectionManager:
    name: str
    creation_name: str
    dtsid: str
    element: ET.Element


class Package(Container):
    """Root ``DTS:Executable`` for a .dtsx file."""

    def __init__(self, name: str, description: str, *, protection_level: int = 0):
        super().__init__(self, "Package", name, "Microsoft.Package", description)  # type: ignore[arg-type]
        self.pkg = self
        self.protection_level = protection_level
        self.connection_managers: list[ConnectionManager] = []
        self.parameters: list[ET.Element] = []
        self.event_handlers: list[EventHandler] = []
        self.version_build = "12"

    # -- connection managers --------------------------------------------- #
    def cm_id(self, name: str) -> str:
        for cm in self.connection_managers:
            if cm.name == name:
                return cm.dtsid
        raise KeyError(f"Unknown connection manager {name!r} in {self.name}")

    def variable_id(self, qualified: str) -> str:
        """Return the DTSID of a ``User::X`` variable or ``$Package::X`` parameter declared anywhere."""
        if qualified.startswith("$Package::"):
            name = qualified.split("::", 1)[1]
            if not any(p.get(d("ObjectName")) == name for p in self.parameters):
                raise KeyError(f"Unknown parameter {qualified!r} in {self.name}")
            return guid(self.name, "param", name)
        namespace, name = qualified.split("::", 1)
        stack: list[Container] = [self]
        while stack:
            scope = stack.pop()
            for v in scope.variables:
                if v.namespace == namespace and v.name == name:
                    return guid(self.name, scope.ref, namespace, name)
            stack.extend(e for e in scope.executables if isinstance(e, Container))
        raise KeyError(f"Unknown variable {qualified!r} in {self.name}")

    def _cm(self, name: str, creation_name: str, description: str = "") -> tuple[ET.Element, str]:
        dtsid = guid(self.name, "cm", name)
        attrs = {d("refId"): f"Package.ConnectionManagers[{name}]", d("CreationName"): creation_name}
        if description:
            attrs[d("Description")] = description
        attrs[d("DTSID")] = dtsid
        attrs[d("ObjectName")] = name
        el = ET.Element(d("ConnectionManager"), attrs)
        self.connection_managers.append(ConnectionManager(name, creation_name, dtsid, el))
        return el, dtsid

    def oledb(self, name: str, server: str, database: str, *, project_param: str | None = None,
              description: str = "") -> str:
        el, _ = self._cm(name, "OLEDB", description)
        if project_param:
            ET.SubElement(el, d("PropertyExpression"), {d("Name"): "ConnectionString"}).text = f"@[$Project::{project_param}]"
        od = ET.SubElement(el, d("ObjectData"))
        ET.SubElement(od, d("ConnectionManager"), {d("ConnectionString"): (
            f"Data Source={server};Initial Catalog={database};Provider=MSOLEDBSQL.1;"
            f"Integrated Security=SSPI;Application Name=SSIS-{self.name}-{name};Auto Translate=False;")})
        return name

    def smtp(self, name: str, server: str) -> str:
        el, _ = self._cm(name, "SMTP")
        od = ET.SubElement(el, d("ObjectData"))
        ET.SubElement(od, d("ConnectionManager"), {d("ConnectionString"):
                                                   f"SmtpServer={server};UseWindowsAuthentication=True;EnableSsl=True;"})
        return name

    def file(self, name: str, path: str, usage: int = 2, expression: str | None = None) -> str:
        """usage: 0 existing file, 1 create file, 2 existing folder, 3 create folder."""
        el, _ = self._cm(name, "FILE")
        if expression:
            ET.SubElement(el, d("PropertyExpression"), {d("Name"): "ConnectionString"}).text = expression
        od = ET.SubElement(el, d("ObjectData"))
        ET.SubElement(od, d("ConnectionManager"), {d("FileUsageType"): str(usage), d("ConnectionString"): path})
        return name

    def flat_file(self, name: str, path: str, columns: Sequence[tuple[str, int, str]], *,
                  expression: str | None = None, header: bool = True, delimiter: str = "_x002C_",
                  code_page: int = 65001, text_qualifier: str | None = "_x0022_", unicode: bool = False) -> str:
        """columns: (name, width, DTS datatype code e.g. 129 str, 130 wstr, 3 i4, 131 numeric)."""
        el, _ = self._cm(name, "FLATFILE")
        if expression:
            ET.SubElement(el, d("PropertyExpression"), {d("Name"): "ConnectionString"}).text = expression
        od = ET.SubElement(el, d("ObjectData"))
        attrs = {d("Format"): "Delimited", d("LocaleID"): "1033", d("Unicode"): str(unicode),
                 d("HeaderRowDelimiter"): "_x000D__x000A_",
                 d("ColumnNamesInFirstDataRow"): str(header), d("RowDelimiter"): ""}
        if text_qualifier:
            attrs[d("TextQualifier")] = text_qualifier
        if not unicode:
            attrs[d("CodePage")] = str(code_page)
        attrs[d("ConnectionString")] = path
        inner = ET.SubElement(od, d("ConnectionManager"), attrs)
        cols = ET.SubElement(inner, d("FlatFileColumns"))
        for i, (col_name, width, dtype) in enumerate(columns):
            last = i == len(columns) - 1
            ET.SubElement(cols, d("FlatFileColumn"), {
                d("ColumnType"): "Delimited", d("ColumnDelimiter"): "_x000D__x000A_" if last else delimiter,
                d("MaximumWidth"): str(width), d("DataType"): str(dtype),
                d("TextQualified"): "True" if text_qualifier else "False", d("ObjectName"): col_name,
                d("DTSID"): guid(self.name, "cm", name, col_name), d("CreationName"): ""})
        return name

    # -- parameters ------------------------------------------------------- #
    def parameter(self, name: str, dtype: str, value: str, *, required: bool = False,
                  sensitive: bool = False, description: str = "") -> str:
        attrs = {d("CreationName"): "", d("DataType"): PARAMETER_TYPES[dtype]}
        if description:
            attrs[d("Description")] = description
        attrs[d("DTSID")] = guid(self.name, "param", name)
        attrs[d("ObjectName")] = name
        if required:
            attrs[d("Required")] = "True"
        if sensitive:
            attrs[d("Sensitive")] = "True"
        el = ET.Element(d("PackageParameter"), attrs)
        prop_attrs = {d("DataType"): PARAMETER_TYPES[dtype], d("Name"): "ParameterValue"}
        if sensitive:
            prop_attrs = {d("Name"): "ParameterValue", "Sensitive": "1", "Encrypted": "1"}
        ET.SubElement(el, d("Property"), prop_attrs).text = value
        self.parameters.append(el)
        return f"$Package::{name}"

    def event_handler(self, event: str) -> EventHandler:
        handler = EventHandler(self, "Package", event)
        self.event_handlers.append(handler)
        return handler

    # -- XML -------------------------------------------------------------- #
    def to_element(self) -> ET.Element:  # type: ignore[override]
        attrs = {
            d("refId"): "Package",
            d("CreationDate"): CREATION_DATE,
            d("CreationName"): "Microsoft.Package",
            d("CreatorComputerName"): CREATOR_COMPUTER,
            d("CreatorName"): CREATOR_NAME,
            d("Description"): self.description,
            d("DTSID"): guid(self.name, "Package"),
            d("ExecutableType"): "Microsoft.Package",
            d("LastModifiedProductVersion"): PRODUCT_VERSION,
            d("LocaleID"): "1033",
            d("ObjectName"): self.name,
            d("PackageType"): "5",
            d("ProtectionLevel"): str(self.protection_level),
            d("VersionBuild"): self.version_build,
            d("VersionGUID"): guid(self.name, "VersionGUID"),
        }
        root = ET.Element(d("Executable"), attrs)
        ET.SubElement(root, d("Property"), {d("Name"): "PackageFormatVersion"}).text = "8"
        for prop, expression in self.property_expressions:
            ET.SubElement(root, d("PropertyExpression"), {d("Name"): prop}).text = expression
        if self.connection_managers:
            cms = ET.SubElement(root, d("ConnectionManagers"))
            cms.extend(cm.element for cm in self.connection_managers)
        if self.parameters:
            params = ET.SubElement(root, d("PackageParameters"))
            params.extend(self.parameters)
        self._fill(root)
        if self.event_handlers:
            handlers = ET.SubElement(root, d("EventHandlers"))
            for handler in self.event_handlers:
                handlers.append(handler.to_element())
        return root

    def to_xml(self) -> str:
        root = self.to_element()
        ET.indent(root, space="  ")
        return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def project_params_xml(params: Iterable[tuple[str, str, str, bool, bool, str]]) -> str:
    """params: (name, dtype, value, required, sensitive, description)."""
    root = ET.Element(f"{{{SSIS_NS}}}Parameters")
    for name, dtype, value, required, sensitive, description in params:
        p = ET.SubElement(root, f"{{{SSIS_NS}}}Parameter", {f"{{{SSIS_NS}}}Name": name})
        props = ET.SubElement(p, f"{{{SSIS_NS}}}Properties")
        for key, val in (("ID", guid("Project.params", name)), ("CreationName", ""), ("Description", description),
                         ("IncludeInDebugDump", "0"), ("Required", "1" if required else "0"),
                         ("Sensitive", "1" if sensitive else "0"), ("Value", value),
                         ("DataType", PARAMETER_TYPES[dtype])):
            attrs = {f"{{{SSIS_NS}}}Name": key}
            if key == "Value" and sensitive:
                attrs.update({"Sensitive": "1", "Encrypted": "1"})
            ET.SubElement(props, f"{{{SSIS_NS}}}Property", attrs).text = val
    ET.indent(root, space="  ")
    return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
