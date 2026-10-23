"""Contract normalisers and the change classifier, on synthetic fixtures."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from sentinel.contracts import jsonshape, openapi, wsdl
from sentinel.contracts.diff import REASONS, diff, overall, summary
from sentinel.contracts.facts import ContractError, Facts
from sentinel.sources.snapshots import SnapshotTooLargeError, make_snapshot
from sentinel.web.i18n import t

SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "servers": [{"url": "https://api.vendor.example/v2"}],
    "components": {
        "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}},
        "schemas": {
            "Payment": {
                "type": "object",
                "required": ["id", "amount"],
                "properties": {
                    "id": {"type": "string"},
                    "amount": {"type": "number"},
                    "currency": {"type": "string", "enum": ["TRY", "EUR", "USD"]},
                },
            },
            "Node": {
                "type": "object",
                "properties": {
                    "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}}
                },
            },
        },
    },
    "security": [{"key": []}],
    "paths": {
        "/payments/{id}": {
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
            "get": {
                "summary": "Ödemeyi getir",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Payment"}}
                        }
                    }
                },
            },
        },
        "/payments": {
            "post": {
                "parameters": [{"name": "dry_run", "in": "query", "schema": {"type": "boolean"}}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Payment"}}
                    },
                },
                "responses": {"201": {"description": "created"}},
            }
        },
        "/tree": {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}
                        }
                    }
                }
            }
        },
    },
}


def changed(mutate: Any) -> list[tuple[str, str]]:
    new = copy.deepcopy(SPEC)
    mutate(new)
    return [(c.severity, c.reason) for c in diff(openapi.normalise(SPEC), openapi.normalise(new))]


def _schema(spec: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = spec["components"]["schemas"]["Payment"]
    return schema


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda s: s["paths"].pop("/tree"), ("breaking", "op_removed")),
        (
            lambda s: s["paths"]["/payments"]["post"]["parameters"].append(
                {"name": "tenant", "in": "query", "required": True, "schema": {"type": "string"}}
            ),
            ("breaking", "required_param_added"),
        ),
        (
            lambda s: _schema(s)["properties"]["currency"]["enum"].remove("USD"),
            ("breaking", "enum_value_removed"),
        ),
        (
            lambda s: _schema(s)["properties"].update({"note": {"type": "string"}}),
            ("non_breaking", "field_added"),
        ),
        (
            lambda s: _schema(s)["properties"]["amount"].update({"type": "string"}),
            ("breaking", "type_changed"),
        ),
        (
            lambda s: _schema(s)["properties"]["id"].update({"nullable": True}),
            ("breaking", "now_nullable"),
        ),
        (
            lambda s: s["servers"].__setitem__(0, {"url": "https://api2.vendor.example/v2"}),
            ("breaking", "server_removed"),
        ),
        (
            lambda s: s["paths"]["/payments/{id}"]["get"].update({"deprecated": True}),
            ("non_breaking", "deprecated"),
        ),
    ],
)
def test_openapi_classification(mutate: Any, expected: tuple[str, str]) -> None:
    assert expected in changed(mutate)


def test_doc_only_changes_are_cosmetic_and_renamed_path_params_are_not_changes() -> None:
    def reword(s: dict[str, Any]) -> None:
        s["paths"]["/payments/{id}"]["get"]["summary"] = "Bir ödemeyi getir"

    assert changed(reword) == [("cosmetic", "doc_changed")]

    def rename(s: dict[str, Any]) -> None:
        item = s["paths"].pop("/payments/{id}")
        item["parameters"][0]["name"] = "paymentId"
        s["paths"]["/payments/{paymentId}"] = item

    assert changed(rename) == []


def test_openapi_reading() -> None:
    facts = {f["id"]: f for f in openapi.normalise(SPEC)}
    assert facts["res:GET /payments/{}:200:$.amount"]["required"] is True
    assert facts["res:GET /payments/{}:200:$.currency"]["enum"] == ["EUR", "TRY", "USD"]
    assert facts["req:POST /payments:$"]["required"] is True
    assert facts["op:POST /payments"]["security"] == ["key"]
    assert facts["res:GET /tree:200:$.children[]"]["type"] == "recursive"  # cycle stopped
    assert "Ödemeyi getir" not in json.dumps(list(facts.values()))  # docs only as a hash
    swagger = openapi.load(
        json.dumps(
            {
                "swagger": "2.0",
                "host": "api.vendor.example",
                "basePath": "/v1",
                "paths": {
                    "/x": {
                        "get": {
                            "responses": {
                                "200": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"a": {"type": "integer"}},
                                    }
                                }
                            }
                        }
                    }
                },
            }
        ).encode()
    )
    ids = {f["id"] for f in openapi.normalise(swagger)}
    assert {"server:https://api.vendor.example/v1", "res:GET /x:200:$.a"} <= ids
    yaml_spec = openapi.load(b"openapi: 3.1.0\npaths: {}\n")
    assert openapi.normalise(yaml_spec) == []


@pytest.mark.parametrize(
    "body",
    [
        b"a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]\nopenapi: 3.0.0\n",  # YAML alias bomb
        b'{"swagger": "1.2"}',
        b"<html>no</html>",
    ],
)
def test_unreadable_specs(body: bytes) -> None:
    with pytest.raises(ContractError):
        openapi.load(body)


def test_json_shape_keeps_types_not_values() -> None:
    secret = "do-not-store-0001"
    value = {"id": secret, "items": [{"a": 1, "b": None}, {"a": 2.5}], "flag": True}
    facts = {f["id"]: f for f in jsonshape.shape(value, "GET /me")}
    assert secret not in json.dumps(list(facts.values()))
    assert facts["res:GET /me:200:$.items[].a"]["type"] == "integer|number"
    assert facts["res:GET /me:200:$.items[].b"]["required"] is False  # missing in one item
    assert facts["res:GET /me:200:$.items[].b"]["nullable"] is True
    assert facts["res:GET /me:200:$.flag"]["required"] is True


WSDL = """<?xml version="1.0"?>
<definitions xmlns="http://schemas.xmlsoap.org/wsdl/" xmlns:soap="http://schemas.xmlsoap.org/wsdl/{soap}/"
  xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:tns="urn:fatura" name="Fatura">
  <types><xs:schema targetNamespace="urn:fatura">
    <xs:complexType name="Invoice"><xs:sequence>
      <xs:element name="uuid" type="xs:string"/>
      <xs:element name="lines" type="tns:Line" maxOccurs="{lines}"/>
      {extra}
    </xs:sequence></xs:complexType>
    <xs:simpleType name="Profile"><xs:restriction base="xs:string">
      <xs:enumeration value="TEMELFATURA"/><xs:enumeration value="TICARIFATURA"/>
    </xs:restriction></xs:simpleType>
  </xs:schema></types>
  <message name="SendRequest"><part name="body" element="tns:Invoice"/></message>
  <portType name="FaturaPort">
    <operation name="Send"><input message="tns:SendRequest"/></operation>
    {operations}
  </portType>
  <service name="FaturaService"><port name="FaturaPort" binding="tns:Binding">
    <soap:address location="https://ws.vendor.example/fatura"/>
  </port></service>
</definitions>"""


def wsdl_facts(
    soap: str = "soap",
    lines: str = "unbounded",
    extra: str = "",
    operations: str = '<operation name="Cancel"/>',
) -> list[dict[str, Any]]:
    body = WSDL.format(soap=soap, lines=lines, extra=extra, operations=operations).encode()
    return wsdl.normalise([wsdl.parse(body)])


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"soap": "soap12"}, ("breaking", "soap_version_changed")),
        ({"operations": ""}, ("breaking", "op_removed")),
        (
            {"extra": '<xs:element name="note" type="xs:string" minOccurs="0"/>'},
            ("non_breaking", "field_added"),
        ),
        (
            {"extra": '<xs:element name="taxId" type="xs:string"/>'},
            ("breaking", "required_field_added"),
        ),
        ({"lines": "1"}, ("breaking", "cardinality_narrowed")),
    ],
)
def test_wsdl_classification(kwargs: dict[str, str], expected: tuple[str, str]) -> None:
    found = [(c.severity, c.reason) for c in diff(wsdl_facts(), wsdl_facts(**kwargs))]
    assert expected in found


def test_wsdl_reading_and_imports() -> None:
    facts = {f["id"]: f for f in wsdl_facts()}
    port = facts["port:FaturaService/FaturaPort"]
    assert (port["soap"], port["address"]) == ("1.1", "https://ws.vendor.example/fatura")
    assert facts["el:Invoice/lines"]["max"] == "unbounded"
    assert facts["enum:Profile"]["values"] == ["TEMELFATURA", "TICARIFATURA"]
    assert facts["msg:SendRequest/body"]["element"] == "Invoice"
    root = wsdl.parse(
        b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        b'<xs:import schemaLocation="common.xsd"/>'
        b'<xs:include schemaLocation="https://elsewhere.example/x.xsd"/></xs:schema>'
    )
    assert wsdl.imports(root, "https://ws.vendor.example/a/fatura.wsdl") == [
        "https://ws.vendor.example/a/common.xsd",
        "https://elsewhere.example/x.xsd",
    ]


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b"<definitions>&x;</definitions>",  # XXE
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        b"<definitions>&b;</definitions>",  # billion laughs
        b"not xml at all",
    ],
)
def test_hostile_xml_is_refused(body: bytes) -> None:
    with pytest.raises(ContractError):
        wsdl.parse(body)


def test_size_limits_refuse_instead_of_truncating() -> None:
    facts = Facts(limit=3)
    for i in range(3):
        facts.add(f"f{i}", "field")
    with pytest.raises(ContractError):
        facts.add("f3", "field")
    huge = [{"id": f"f{i}", "t": "field", "pad": "x" * 200} for i in range(15_000)]
    with pytest.raises(SnapshotTooLargeError):
        make_snapshot(huge, strict=True)


def test_every_reason_has_copy_and_summaries_add_up() -> None:
    for reason in REASONS:
        assert t(f"contract_reasons.{reason}")
    variants = [wsdl_facts(soap="soap12"), wsdl_facts(lines="1"), wsdl_facts(operations="")]
    for variant in variants:
        assert {c.reason for c in diff(wsdl_facts(), variant)} <= REASONS
    changes = diff(wsdl_facts(), wsdl_facts(soap="soap12", operations=""))
    assert overall(changes) == "breaking"
    assert overall([]) is None
    report = summary(changes)
    assert report["counts"]["breaking"] == len(changes)
    assert "Cancel" in report["operations"]


def test_schema_composition_and_odd_references() -> None:
    spec = {
        "openapi": "3.1.0",
        "components": {
            "schemas": {
                "Base": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string"}},
                }
            }
        },
        "paths": {
            "/x": {
                "post": {
                    "requestBody": {
                        "content": {
                            "text/plain": {
                                "schema": {
                                    "allOf": [{"$ref": "#/components/schemas/Base"}],
                                    "properties": {
                                        "kind": {
                                            "oneOf": [{"type": "string"}, {"type": "integer"}]
                                        },
                                        "tags": {
                                            "type": "object",
                                            "additionalProperties": {"type": ["string", "null"]},
                                        },
                                        "remote": {"$ref": "https://elsewhere.example/schema.json"},
                                        "lost": {"$ref": "#/components/schemas/Missing"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {
                        "204": {"description": "no body"},
                        "400": {"$ref": "#/components/responses/Bad"},
                    },
                }
            }
        },
    }
    facts = {f["id"]: f for f in openapi.normalise(spec)}
    assert facts["req:POST /x:$.id"]["required"] is True  # merged from allOf
    assert facts["req:POST /x:$.kind"]["type"] == "oneOf(integer|string)"  # order-free
    assert facts["req:POST /x:$.tags.*"]["nullable"] is True  # 3.1 type list with null
    assert facts["req:POST /x:$.remote"]["type"] == "external_ref"  # recorded, not fetched
    assert facts["req:POST /x:$.lost"]["type"] == "broken_ref"
    assert "status:POST /x:204" in facts
    swagger = {
        "swagger": "2.0",
        "paths": {
            "/y": {
                "put": {
                    "parameters": [
                        {
                            "name": "body",
                            "in": "body",
                            "required": True,
                            "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    assert "req:PUT /y:$.n" in {f["id"] for f in openapi.normalise(swagger)}

    schema = wsdl.parse(
        b'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        b'<xs:element name="Order"><xs:complexType><xs:sequence>'
        b'<xs:element name="id" type="xs:string" minOccurs="0" nillable="true"/>'
        b"</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    xsd = {f["id"]: f for f in wsdl.normalise([schema])}
    assert xsd["el:Order"]["type"] == "complex"
    assert (xsd["el:Order/id"]["required"], xsd["el:Order/id"]["nullable"]) == (False, True)
