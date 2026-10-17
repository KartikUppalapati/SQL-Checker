"""Load a PostgreSQL DDL file into a catalog of tables, columns, keys and enum types.

Identifiers are normalized with PostgreSQL rules: unquoted names fold to lowercase,
quoted names keep their exact spelling.
"""
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

_ENUM_RE = re.compile(
    r"^\s*TYPE\s+((?:\"[^\"]+\"|\w+)(?:\.(?:\"[^\"]+\"|\w+))?)\s+AS\s+ENUM\s*\((.*)\)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def norm_ident(ident: exp.Identifier | str, quoted: bool = False) -> str:
    if isinstance(ident, exp.Identifier):
        return ident.this if ident.quoted else ident.this.lower()
    return ident if quoted else ident.lower()


def _norm_raw(name: str) -> str:
    """Normalize a raw (possibly double-quoted) identifier string."""
    name = name.split(".")[-1]
    if name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
    return name.lower()


@dataclass
class Column:
    name: str
    type: str
    not_null: bool = False
    enum_labels: list[str] | None = None  # set when the column's type is an ENUM


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


@dataclass
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    primary_key: list[str] = field(default_factory=list)
    unique: list[list[str]] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)


@dataclass
class Catalog:
    tables: dict[str, Table] = field(default_factory=dict)
    enums: dict[str, list[str]] = field(default_factory=dict)

    def sqlglot_schema(self) -> dict[str, dict[str, str]]:
        """Mapping usable by sqlglot's optimizer (column -> type string)."""
        return {t.name: {c.name: c.type for c in t.columns.values()} for t in self.tables.values()}


def _col_names(node: exp.Expression) -> list[str]:
    return [norm_ident(i) for i in node.find_all(exp.Identifier)]


def _type_str(kind: exp.DataType | None) -> str:
    if kind is None:
        return "TEXT"
    if kind.this == exp.DataType.Type.USERDEFINED:
        return "TEXT"  # enum and other user types: compared as text labels
    return kind.sql(dialect="postgres")


def _enum_labels(kind: exp.DataType | None, enums: dict[str, list[str]]) -> list[str] | None:
    if kind is None or kind.this != exp.DataType.Type.USERDEFINED:
        return None
    name = str(kind.args.get("kind") or "").split(".")[-1].strip('"')
    # sqlglot drops the quotes from the type name: try the exact spelling, then the folded one
    return enums.get(name) or enums.get(name.lower())


def load_catalog(ddl: str) -> Catalog:
    cat = Catalog()
    for stmt in sqlglot.parse(ddl, read="postgres"):
        if stmt is None:
            continue
        if isinstance(stmt, exp.Command) and stmt.this.upper() == "CREATE":
            m = _ENUM_RE.match(str(stmt.expression))
            if m:
                labels = re.findall(r"'((?:[^']|'')*)'", m.group(2))
                cat.enums[_norm_raw(m.group(1))] = [l.replace("''", "'") for l in labels]
            continue
        if not (isinstance(stmt, exp.Create) and isinstance(stmt.this, exp.Schema)):
            continue
        tbl_name = norm_ident(stmt.this.this.this)
        table = Table(tbl_name)
        for item in stmt.this.expressions:
            if isinstance(item, exp.ColumnDef):
                kind = item.args.get("kind")
                col = Column(norm_ident(item.this), _type_str(kind), enum_labels=_enum_labels(kind, cat.enums))
                for c in item.constraints:
                    kind = c.kind
                    if isinstance(kind, exp.NotNullColumnConstraint) and not kind.args.get("allow_null"):
                        col.not_null = True
                    elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
                        col.not_null = True
                        table.primary_key = [col.name]
                    elif isinstance(kind, exp.UniqueColumnConstraint):
                        table.unique.append([col.name])
                    elif isinstance(kind, exp.Reference):
                        ref = kind.this  # Schema(this=Table, expressions=[cols]) or Table
                        ref_tbl = ref.this if isinstance(ref, exp.Schema) else ref
                        ref_cols = [norm_ident(e) for e in ref.expressions] if isinstance(ref, exp.Schema) else []
                        table.foreign_keys.append(ForeignKey([col.name], norm_ident(ref_tbl.this), ref_cols))
                table.columns[col.name] = col
            elif isinstance(item, exp.PrimaryKey):
                table.primary_key = _col_names(exp.Tuple(expressions=item.expressions))
            elif isinstance(item, exp.UniqueColumnConstraint):
                table.unique.append(_col_names(item.this) if item.this else [])
            elif isinstance(item, exp.ForeignKey):
                ref = item.args["reference"].this
                ref_tbl = ref.this if isinstance(ref, exp.Schema) else ref
                ref_cols = [norm_ident(e) for e in ref.expressions] if isinstance(ref, exp.Schema) else []
                table.foreign_keys.append(
                    ForeignKey([norm_ident(e) for e in item.expressions], norm_ident(ref_tbl.this), ref_cols))
        for pk_col in table.primary_key:
            if pk_col in table.columns:
                table.columns[pk_col].not_null = True
        # FKs that omit the referenced columns point at the referenced PK
        cat.tables[tbl_name] = table
    for t in cat.tables.values():
        for fk in t.foreign_keys:
            if not fk.ref_columns and fk.ref_table in cat.tables:
                fk.ref_columns = list(cat.tables[fk.ref_table].primary_key)
    return cat
