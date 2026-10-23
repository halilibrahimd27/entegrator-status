"""WSDL 1.1 and XSD → facts, parsed with ``defusedxml`` (no DTDs, no entities).

Kept: services and ports with their endpoint address and SOAP version, the operations
of each port type with their messages, message parts, and every XSD element of named or
global complex types with its type and cardinality (``min`` / ``max``), plus simple-type
enumerations. Imports and includes are returned as URLs for the caller to fetch: only on
the same host, at most three levels deep (the adapter enforces both).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin
from xml.etree.ElementTree import Element  # nosec B405 - the type only; parsing is defusedxml

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from sentinel.contracts.facts import ContractError, Facts, enum_of

WSDL = "http://schemas.xmlsoap.org/wsdl/"
SOAP11 = "http://schemas.xmlsoap.org/wsdl/soap/"
SOAP12 = "http://schemas.xmlsoap.org/wsdl/soap12/"
XSD = "http://www.w3.org/2001/XMLSchema"


def _q(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


def _local(name: str | None) -> str:
    """``tns:Order`` → ``Order`` (prefixes vary between documents; names do not)."""
    return (name or "").rsplit(":", 1)[-1]


def parse(body: bytes) -> Element:
    try:
        return fromstring(body, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise ContractError(f"unreadable XML: {type(exc).__name__}") from None


def imports(root: Element, base: str) -> list[str]:
    found = []
    for tag, attr in (
        (_q(XSD, "import"), "schemaLocation"),
        (_q(XSD, "include"), "schemaLocation"),
        (_q(WSDL, "import"), "location"),
    ):
        for node in root.iter(tag):
            if node.get(attr):
                found.append(urljoin(base, str(node.get(attr))))
    return found


def _cardinality(node: Element) -> tuple[int, str]:
    low = node.get("minOccurs", "1")
    high = node.get("maxOccurs", "1")
    return (int(low) if low.isdigit() else 1), high


def _schema(schema: Element, facts: Facts) -> None:
    for complex_type in schema.iter(_q(XSD, "complexType")):
        owner = complex_type.get("name")
        if owner is None:
            continue  # anonymous types are reached through their element below
        _elements(complex_type, owner, facts)
    for element in schema.findall(_q(XSD, "element")):  # global elements
        name = element.get("name")
        if not name:
            continue
        facts.add(f"el:{name}", "field", dir="both", type=_local(element.get("type")) or "complex")
        inline = element.find(_q(XSD, "complexType"))
        if inline is not None:
            _elements(inline, name, facts)
    for simple in schema.iter(_q(XSD, "simpleType")):
        name = simple.get("name")
        values = [e.get("value") for e in simple.iter(_q(XSD, "enumeration"))]
        if name and values:
            facts.add(f"enum:{name}", "enum", values=enum_of(values))


def _elements(container: Element, owner: str, facts: Facts) -> None:
    for element in container.iter(_q(XSD, "element")):
        name = element.get("name") or _local(element.get("ref"))
        if not name:
            continue
        low, high = _cardinality(element)
        facts.add(
            f"el:{owner}/{name}",
            "field",
            dir="both",
            op=owner,
            type=_local(element.get("type")) or ("complex" if len(element) else "any"),
            min=low,
            max=high,
            required=low > 0,
            nullable=element.get("nillable") == "true",
        )


def normalise(documents: list[Element]) -> list[dict[str, Any]]:
    """Facts of a WSDL and the schemas it imports (already fetched by the caller)."""
    facts = Facts()
    for root in documents:
        for service in root.iter(_q(WSDL, "service")):
            for port in service.iter(_q(WSDL, "port")):
                address, version = None, None
                for ns, soap in ((SOAP11, "1.1"), (SOAP12, "1.2")):
                    found = port.find(_q(ns, "address"))
                    if found is not None:
                        address, version = found.get("location"), soap
                facts.add(
                    f"port:{service.get('name')}/{port.get('name')}",
                    "port",
                    address=address,
                    soap=version,
                    binding=_local(port.get("binding")),
                )
        for port_type in root.iter(_q(WSDL, "portType")):
            for operation in port_type.iter(_q(WSDL, "operation")):
                op = str(operation.get("name"))
                messages = {
                    kind: _local(node.get("message"))
                    for kind in ("input", "output")
                    if (node := operation.find(_q(WSDL, kind))) is not None
                }
                facts.add(f"op:{op}", "op", op=op, **messages)
        for message in root.iter(_q(WSDL, "message")):
            for part in message.iter(_q(WSDL, "part")):
                facts.add(
                    f"msg:{message.get('name')}/{part.get('name')}",
                    "msg",
                    element=_local(part.get("element") or part.get("type")),
                )
        for schema in root.iter(_q(XSD, "schema")):
            _schema(schema, facts)
    return facts.items()
